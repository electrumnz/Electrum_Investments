"""Keyless sources the dreamer can be researched against, and no search engine.

`research.py` describes a researcher that goes and looks; `deploy/run-research.sh`
gives it a quarantined Hermes instance to look from. Measured on the droplet
17 Aug 2026, that instance holds **no web tools at all**: Hermes' `web` toolset
comes through the Nous Portal Tool Gateway, which is a paid subscription this
account does not have. The plumbing is correct and the feature was inert.

This is the answer that fits the constraint the operator actually has — one
provider, DigitalOcean, no new vendor and no new card. It turns out most of
what a dreamer needs is reachable without any key at all:

- **EDGAR full-text search** for company and financial claims. Free, official,
  no key, 10 requests/second, and a *primary* source with a permanent URL —
  better provenance than a news article paraphrasing the same filing. Measured
  working: a search for CommScope's net debt returned 27 filings.
- **RSS** for current news, from whichever outlets `config/rules.yaml` lists.
  Free and keyless at every major publisher.
- **Wikipedia** for reference claims, and more usefully as a ROUTE to primary
  sources, since its articles carry citations.
- **A page we already have the URL for.** Since the Marketaux parser stopped
  discarding attribution, every headline the loop fetches carries a link.

**What is deliberately absent is open-web search.** It needs an index, every
index needs a key or a card, and the keyless route — scraping a search engine
— was measured returning an HTTP 202 challenge page with zero results. Driving
a browser past that block would be an arms race that fails SILENTLY: one day a
challenge page arrives, no citations parse, hops stay unchecked and nothing
says why. That is the failure mode this repository is shaped to refuse, and it
is not worth 500 MB of Chromium on a 1.9 GB box shared with the trading loop.

So the gap is real and stated rather than papered over: a claim nobody in
these four sources has published cannot be sourced here, and the hop stays
unchecked. That is the honest answer, and it is the same answer the dreamer
already lives with.

## Quotes are EXTRACTED, never generated

This is the property that makes this module stronger than the Hermes path it
stands in for, and it is worth being explicit about because it inverts a
trade-off recorded elsewhere.

`research.Citation` exists because **a summary launders provenance**, and the
rail against it has so far been a prompt plus a strict parse: the researcher
is *asked* for quotations and anything unparseable is dropped. That works, and
it still rests on a model choosing to transcribe rather than paraphrase — a
model can fabricate a quotation and attach a real URL to it, and nothing
downstream can tell.

Nothing here asks a model for anything. A citation's `quote` is a **literal
substring of the document at its `url`**, selected by term overlap in
`extract_passages`. Fabrication is not unlikely; it is unavailable. The cost is
that the quotes are blunter than a model would choose — a sentence that
mentions the terms rather than the sentence that best answers the question —
and that is a trade worth making for a field whose entire value is that
somebody else wrote it.

**Do not "improve" this by having a model pick the quotes.** The moment a model
sits between the page and the `Citation`, the guarantee is back to being a
prompt, and this module's reason for existing is gone.

## Everything here touches the network, so nothing here may gate

Same rule as every feed: `RiskGate` must stay deterministic and must not fail
open on a network call, and `docs/HANDOFF.md` requires that nothing web-derived
becomes a gating input. This module is imported by `dreamer.py` and by nothing
in the trading path. Every failure answers with an empty result and a reason,
never an exception, because the caller is a scheduled job that must not die of
a look-up.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

import structlog

from .data._http import DEFAULT_TIMEOUT_SECONDS, TextGetter, TtlCache, httpx_text_getter
from .research import (
    MAX_CITATIONS_PER_QUESTION,
    MAX_QUOTE_CHARS,
    Citation,
    ResearchAnswer,
    clean_citations,
)

log = structlog.get_logger(__name__)

# ----------------------------------------------------------------- who we are
#
# **The SEC requires this and enforces it**, which is why it is a constant here
# rather than a default buried in a getter. Their access policy asks for a
# declared user agent naming a person and a contact address; requests without
# one are refused, and a generic library string is the shape they block.
#
# It is the operator's address because that is who is asking. A support desk
# reading a log needs somebody to write to, and a bot with an unreachable
# contact is the thing rate limits exist to stop.
CONTACT = "electrum.nz@gmail.com"
USER_AGENT = f"Mudhorn Capital (personal trading research) {CONTACT}"

#: Their published ceiling is 10 requests/second. Nothing here approaches that
#: — the dreamer asks at most three questions once a day — so this is a floor
#: under a mistake rather than a tuned figure. A loop that started hammering
#: EDGAR would be a bug, and this bounds what that bug costs them.
EDGAR_MIN_SECONDS_BETWEEN = 0.5

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

#: Filings and pages are cached for the day. A 10-Q does not change once filed,
#: and the dreamer revisits the same chain across runs, so re-reading the same
#: document is spending somebody else's bandwidth to learn nothing.
DOCUMENT_CACHE_SECONDS = 86_400.0

#: RSS is the opposite case and the TTL says so: the whole value of a feed is
#: that it is current, and a headline cached for a day is a headline that has
#: stopped being news.
FEED_CACHE_SECONDS = 900.0

#: How many documents one question may open. Each is a network round trip and a
#: multi-megabyte read, and a question that needs more than this to answer is a
#: question the four sources cannot answer.
MAX_DOCUMENTS_PER_QUESTION = 3

#: How many of a question's distinct terms a sentence or headline must carry
#: before it counts as a match.
#:
#: **One is not enough, and that was measured rather than reasoned about.**
#: Asked "Corning optical fibre market share", the first live run cited a BBC
#: article about SK Hynix retail investors — matched on the single word
#: "share", and the quote was two sentences of an unrelated human-interest
#: piece. Every part of that citation was true: a real substring, a real
#: publisher, a working URL. It was also worthless, and worse than worthless
#: because a dreamer might cite it and mark a hop checked.
#:
#: A literal quote with a real link is not automatically evidence. Requiring
#: two distinct terms is the cheapest filter that kills the coincidence
#: without needing a model to judge relevance — which is the thing this module
#: exists to avoid.
MIN_TERM_MATCHES = 2

#: How far back EDGAR full-text search is allowed to reach, in days.
#:
#: **Measured, and it matters more than it looks.** Asked about Corning's fibre
#: market share, the first live run returned a genuinely good sentence — "the
#: world's largest manufacturer of optical fiber" — out of a fund report filed
#: in **2001**. A real quote, a real primary source, a working permanent URL,
#: and twenty-five years stale for a present-tense claim.
#:
#: EDGAR's full-text index starts in 2001 and its relevance ranking has no
#: opinion about age, so without a window the oldest filings compete with this
#: quarter's on equal terms. Three years keeps several annual cycles in range
#: while making "is" claims answerable with something recent.
#:
#: The filing DATE is on every citation's publisher line regardless, so a
#: reader always sees how old the evidence is even when this window is widened.
EDGAR_LOOKBACK_DAYS = 1095


# --------------------------------------------------------------- reading text


class _Stripper(HTMLParser):
    """HTML to text, using the standard library and no new dependency.

    Deliberately crude, and honest about it. It drops tags and keeps character
    data; it does not understand layout, tables or `display:none`. That is
    adequate here because the output is only ever searched for sentences
    containing the question's terms — a citation is a literal substring, so a
    slightly ragged extraction costs a clumsy quote rather than a wrong one.

    `script` and `style` contents are dropped, because leaving them in puts
    JavaScript source into a quotation, which is the one way this could produce
    something that looks like prose and is not.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


def to_text(document: str) -> str:
    """Readable text from HTML, XML or plain text. Never raises.

    A malformed document is common and is not an error worth propagating: the
    parser keeps whatever it managed to read, which is the same direction every
    feed adapter here takes.
    """
    stripper = _Stripper()
    try:
        stripper.feed(document)
        stripper.close()
    except Exception:  # pragma: no cover - defensive, the parser is lenient
        pass
    text = stripper.text()
    return text or " ".join(document.split())


# ------------------------------------------------------- extracting the quote

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for", "from", "had", "has", "have", "how", "in", "is", "it", "its", "of", "on", "or", "that", "the", "their", "there", "this", "to", "was", "were", "what", "when", "where", "which", "who", "why", "will", "with", "would"]
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def terms_of(question: str) -> list[str]:
    """The words worth matching on. Lowercased, stopwords and stubs removed."""
    words = re.findall(r"[A-Za-z0-9$%.]+", question.lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def extract_passages(text: str, question: str, *, limit: int) -> list[str]:
    """Sentences from `text` that best match `question`, as literal substrings.

    **The heart of this module, and the reason it can promise what the Hermes
    path cannot.** Every string returned is a slice of the document, so a
    citation built from one cannot be a fabrication — at worst it is a badly
    chosen sentence, which a reader following the URL will see immediately.

    Scored by how many DISTINCT question terms a sentence contains rather than
    by total occurrences, so a page repeating one word forty times does not
    outrank a sentence that actually addresses the question. Ties break towards
    the earlier sentence, because documents put their claims before their
    boilerplate.

    A sentence matching nothing is never returned. An empty result is a real
    answer — "this document does not discuss it" — and manufacturing a
    plausible sentence to avoid one is the failure this whole arrangement is
    built against.
    """
    wanted = set(terms_of(question))
    if not wanted:
        return []

    scored: list[tuple[int, int, str]] = []
    for position, raw in enumerate(_SENTENCE.split(text)):
        sentence = " ".join(raw.split())
        # Long enough to carry a claim, short enough to stay a quotation. A
        # fragment is not evidence and a page is not a quote.
        if not 40 <= len(sentence) <= MAX_QUOTE_CHARS * 2:
            continue
        lowered = sentence.lower()
        hits = sum(1 for term in wanted if term in lowered)
        # A single shared word is a coincidence, not a match. See
        # MIN_TERM_MATCHES for the citation that taught this.
        if hits >= min(MIN_TERM_MATCHES, len(wanted)):
            scored.append((-hits, position, sentence))

    scored.sort()
    return [sentence for _, _, sentence in scored[:limit]]


# ------------------------------------------------------------------- a source


@dataclass(frozen=True)
class Lookup:
    """What one source came back with, and what went wrong if anything.

    `error` and an empty `citations` are different findings, the same
    distinction `ResearchAnswer` draws one layer up: a source that was asked
    and had nothing, and a source that could not be reached, say opposite
    things about the world.
    """

    citations: list[Citation] = field(default_factory=list)
    error: str = ""
    #: Whether this source actually went and looked. False when there was
    #: nothing for it to look AT — no feeds configured, no terms to match —
    #: which is a THIRD state and not a success.
    #:
    #: It exists because collapsing it into success got the outage case wrong:
    #: with no RSS feeds set, a total network failure counted two failures out
    #: of three sources and reported as "answered, found nothing", which is a
    #: claim about the world made from a claim about the network. Caught by a
    #: test rather than on the box.
    attempted: bool = True

    @property
    def ok(self) -> bool:
        return not self.error


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Sources:
    """The four keyless sources, sharing one getter and one clock.

    Injected rather than constructed, so no test touches the network — the same
    contract every adapter in `data/` follows.
    """

    getter: TextGetter | None = None
    now: Callable[[], datetime] = _now
    #: Injected so the throttle is testable without sleeping. `time.monotonic`
    #: rather than the wall clock: a clock step backwards must not turn a
    #: rate limit into a stall.
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    feeds: Sequence[str] = ()
    _documents: TtlCache = field(init=False)
    _feeds: TtlCache = field(init=False)
    _last_sec_request: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._documents = TtlCache(ttl_seconds=DOCUMENT_CACHE_SECONDS)
        self._feeds = TtlCache(ttl_seconds=FEED_CACHE_SECONDS)
        if self.getter is None:
            self.getter = httpx_text_getter(
                timeout=DEFAULT_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            )

    # -- reading one document -------------------------------------------------

    def _throttle(self, url: str) -> None:
        """Space out requests to sec.gov, because they ask and they enforce.

        Applied by HOST rather than globally: Wikipedia and a news site have
        their own budgets and should not wait on the SEC's. Nothing here comes
        near ten a second — three questions once a day — so this is a floor
        under a bug rather than a tuned figure, and a bug that starts hammering
        a public endpoint is exactly what a floor is for.
        """
        if "sec.gov" not in url:
            return
        waited = self.clock() - self._last_sec_request
        if 0 <= waited < EDGAR_MIN_SECONDS_BETWEEN:
            self.sleep(EDGAR_MIN_SECONDS_BETWEEN - waited)
        self._last_sec_request = self.clock()

    def read(self, url: str, publisher: str, question: str) -> Lookup:
        """Open a URL we already have and quote what it says about `question`.

        The simplest source and, for a news-sparked hop, the most direct: the
        headline that started the chain now arrives with its link attached, so
        the page the dreamer is reasoning about is one fetch away.
        """
        assert self.getter is not None
        cached, value = self._documents.get()
        if cached and isinstance(value, dict) and url in value:
            document = str(value[url])
        else:
            self._throttle(url)
            try:
                document = self.getter(url, {})
            except Exception as exc:
                log.warning("lookup_fetch_failed", url=url, error=repr(exc))
                return Lookup(error=f"could not read {url}: {exc}")
            held = dict(value) if isinstance(value, dict) else {}
            held[url] = document
            self._documents.put(held)

        try:
            passages = extract_passages(to_text(document), question, limit=2)
        except Exception as exc:  # pragma: no cover - the parser is lenient
            log.warning("lookup_extract_failed", url=url, error=repr(exc))
            return Lookup(error=f"could not read {url}: {exc}")
        stamp = self.now()
        return Lookup(
            citations=[
                Citation(quote=p, url=url, retrieved_at=stamp, publisher=publisher)
                for p in passages
            ]
        )

    # -- EDGAR ----------------------------------------------------------------

    def edgar(self, question: str, *, entity: str = "") -> Lookup:
        """Company and financial claims, from filings.

        Two round trips by necessity: full-text search returns metadata with no
        snippet — measured, not assumed — so the filing itself has to be opened
        to produce a quotation. `MAX_DOCUMENTS_PER_QUESTION` bounds that.

        The URL is CONSTRUCTED from the accession number and document name in
        the hit rather than read from a field, because EDGAR's response carries
        no href. That construction is the one place here that could produce a
        link to nothing, so `_edgar_url` returns empty on anything it cannot
        build and the hit is skipped rather than cited.
        """
        assert self.getter is not None
        # **Terms, never the whole question as a phrase.** The first live run
        # sent `"CommScope is carrying heavy debt"` as an exact-phrase search
        # and EDGAR returned nothing — no filing contains that sentence,
        # because it is the dreamer's wording and not anybody's disclosure.
        # A hop's claim is always phrased by the dreamer, so phrase-searching
        # it is guaranteed to miss.
        #
        # The terms are joined unquoted, which EDGAR reads as "any of these",
        # ranked by relevance. Blunter than a phrase and it actually returns
        # filings, which is the whole job.
        terms = terms_of(question)
        if not terms:
            return Lookup(attempted=False)
        params = {"q": " ".join(terms[:8])}
        if entity:
            params["entityName"] = entity
        # EDGAR ORs unquoted terms and ranks by relevance with no regard for
        # age, so the window is what stops a 2001 filing answering a question
        # about this quarter. `_edgar_window` is pure so the dates are testable
        # without freezing a clock inside this method.
        params.update(_edgar_window(self.now()))

        try:
            self._throttle(EDGAR_SEARCH_URL)
            raw = self.getter(EDGAR_SEARCH_URL, params)
            import json

            payload = json.loads(raw)
        except Exception as exc:
            log.warning("edgar_search_failed", error=repr(exc))
            return Lookup(error=f"EDGAR search failed: {exc}")

        hits = (payload.get("hits") or {}).get("hits") or []
        if not hits:
            return Lookup()

        citations: list[Citation] = []
        for hit in hits[:MAX_DOCUMENTS_PER_QUESTION]:
            url = _edgar_url(hit)
            if not url:
                continue
            source = hit.get("_source") or {}
            names = source.get("display_names") or ["SEC EDGAR"]
            form = source.get("form", "filing")
            filed = source.get("file_date", "")
            publisher = f"{names[0]} {form} {filed}".strip()
            found = self.read(url, publisher, question)
            citations.extend(found.citations)
            if len(citations) >= MAX_CITATIONS_PER_QUESTION:
                break
        return Lookup(citations=citations)

    # -- Wikipedia ------------------------------------------------------------

    def wikipedia(self, question: str) -> Lookup:
        """Reference claims, and a route to the primary sources it cites.

        Quoted as Wikipedia, never laundered into "it is known that": the
        publisher on every citation names the encyclopedia, so a dreamer citing
        one is visibly citing a tertiary source and the operator reading the
        chain can weigh it accordingly.
        """
        assert self.getter is not None
        params = {
            "action": "query",
            "list": "search",
            "srsearch": question,
            "srlimit": "2",
            "format": "json",
        }
        try:
            import json

            payload = json.loads(self.getter(WIKIPEDIA_API, params))
        except Exception as exc:
            log.warning("wikipedia_search_failed", error=repr(exc))
            return Lookup(error=f"Wikipedia search failed: {exc}")

        results = ((payload.get("query") or {}).get("search")) or []
        citations: list[Citation] = []
        for result in results[:2]:
            title = str(result.get("title") or "").strip()
            if not title:
                continue
            url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
            found = self.read(url, f"Wikipedia: {title}", question)
            citations.extend(found.citations)
        return Lookup(citations=citations)

    # -- RSS ------------------------------------------------------------------

    def news(self, question: str) -> Lookup:
        """Current news from the configured feeds.

        Matches the question's terms against headlines FIRST and only then
        opens the article, because a feed carries dozens of items and opening
        all of them to find the one that matches would spend a publisher's
        bandwidth on a keyword search we can do locally.
        """
        assert self.getter is not None
        if not self.feeds:
            return Lookup(attempted=False)

        wanted = set(terms_of(question))
        if not wanted:
            return Lookup(attempted=False)

        cached, value = self._feeds.get()
        items: list[tuple[str, str, str]] = list(value) if cached else []
        if not cached:
            for feed in self.feeds:
                try:
                    items.extend(parse_rss(self.getter(feed, {}), feed))
                except Exception as exc:
                    # One bad feed costs its own items and not the others'.
                    log.warning("rss_failed", feed=feed, error=repr(exc))
            self._feeds.put(items)

        floor = min(MIN_TERM_MATCHES, len(wanted))
        ranked = sorted(
            (
                (-sum(1 for t in wanted if t in title.lower()), title, url, publisher)
                for title, url, publisher in items
            ),
        )
        citations: list[Citation] = []
        for score, _title, url, publisher in ranked[:MAX_DOCUMENTS_PER_QUESTION]:
            # Headlines are short, so a one-word overlap here is even weaker
            # evidence than in body text. Same floor, applied before the fetch
            # so a coincidence does not cost a round trip either.
            if -score < floor:
                break
            found = self.read(url, publisher, question)
            citations.extend(found.citations)
            if len(citations) >= MAX_CITATIONS_PER_QUESTION:
                break
        return Lookup(citations=citations)


def _edgar_window(now: datetime) -> dict[str, str]:
    """The `dateRange` parameters restricting a search to recent filings."""
    from datetime import timedelta

    start = now - timedelta(days=EDGAR_LOOKBACK_DAYS)
    return {
        "dateRange": "custom",
        "startdt": start.date().isoformat(),
        "enddt": now.date().isoformat(),
    }


def _edgar_url(hit: object) -> str:
    """A filing's document URL, or empty when it cannot be built.

    EDGAR's `_id` is `<accession>:<document>`, and the archive path wants the
    accession with its hyphens stripped and the CIK without leading zeros.
    Every one of those is a chance to build a link to nothing, so anything
    unexpected answers empty and the caller skips the hit — a citation pointing
    at a 404 is worse than no citation, because it looks checked.
    """
    if not isinstance(hit, dict):
        return ""
    raw_id = str(hit.get("_id") or "")
    source = hit.get("_source") or {}
    if ":" not in raw_id or not isinstance(source, dict):
        return ""
    accession, document = raw_id.split(":", 1)
    ciks = source.get("ciks") or []
    if not document or not ciks:
        return ""
    return EDGAR_ARCHIVE.format(
        cik=str(ciks[0]).lstrip("0"),
        accession=accession.replace("-", ""),
        document=document,
    )


_ITEM = re.compile(r"<item[^>]*>(.*?)</item>", re.S | re.I)
_ENTRY = re.compile(r"<entry[^>]*>(.*?)</entry>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S | re.I)
_LINK = re.compile(r"<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", re.S | re.I)
_LINK_HREF = re.compile(r'<link[^>]*href="([^"]+)"', re.I)


def parse_rss(document: str, feed_url: str) -> list[tuple[str, str, str]]:
    """(title, url, publisher) per item. Tolerant of RSS and Atom alike.

    Regex rather than an XML parser, deliberately: feeds in the wild are
    frequently not well-formed, and a strict parse throws away every item in a
    feed because one of them has an unescaped ampersand. Same reasoning as
    every `_parse` in `data/` — a feed that changes shape should cost items,
    not raise.

    Whitespace is collapsed on the way out for the reason `marketaux._parse`
    does it: these become bullets in a document a model reads, and a newline in
    a title can close the bullet and open its own section.
    """
    publisher = re.sub(r"^https?://(www\.)?", "", feed_url).split("/")[0]
    out: list[tuple[str, str, str]] = []
    for block in _ITEM.findall(document) + _ENTRY.findall(document):
        title_match = _TITLE.search(block)
        if not title_match:
            continue
        title = " ".join(re.sub(r"<[^>]+>", "", title_match.group(1)).split())
        link_match = _LINK_HREF.search(block) or _LINK.search(block)
        url = " ".join(link_match.group(1).split()) if link_match else ""
        if title and url.startswith(("http://", "https://")):
            out.append((title, url, publisher))
    return out


# ------------------------------------------------------------ the researcher


@dataclass
class LocalResearcher:
    """Answers a question from the keyless sources. Satisfies `dreamer._Asker`.

    Drops into the existing `research_dream` path unchanged, so everything that
    already holds keeps holding: the answers are stored as `citation` messages,
    the next step shows them to the dreamer, and **nothing here marks a hop
    checked**. Matching a quotation to a claim stays the model's judgement.

    `enabled` is True unconditionally, and that is not the shortcut it looks
    like. The Hermes researcher reads `permitted` because its wrapper may not
    be granted; this one needs no grant, no credential and no subscription — it
    is `httpx` against public endpoints. What can still fail is the network,
    and that fails per-question into an `error` on the answer, which is where
    it belongs. Claiming to be disabled because a fetch might fail would report
    "nobody looked" for a look-up that did happen and found nothing.
    """

    sources: Sources = field(default_factory=Sources)

    @property
    def enabled(self) -> bool:
        return True

    def ask(self, question: str) -> ResearchAnswer:
        """Put one question to every source and merge what comes back.

        Every source is asked rather than one being chosen, because choosing
        means classifying the question — and a classifier that sent a filings
        question to Wikipedia would fail invisibly, returning a plausible
        tertiary quote instead of the primary one. Four cheap look-ups beat one
        clever routing decision.

        Ordered EDGAR first so the primary source wins the cap when several
        sources answer. `clean_citations` applies the cap last, after the
        unattributable are dropped, so a filing is never crowded out by an
        encyclopedia.
        """
        text = question.strip()
        if not text:
            return ResearchAnswer(question=question, error="empty question")

        found: list[Citation] = []
        errors: list[str] = []
        attempted = 0
        for name, lookup in (
            ("edgar", self.sources.edgar(text)),
            ("news", self.sources.news(text)),
            ("wikipedia", self.sources.wikipedia(text)),
        ):
            if not lookup.attempted:
                # Nothing for it to look at. Neither a success nor a failure,
                # and counting it as either is how the outage case goes wrong.
                continue
            attempted += 1
            if lookup.ok:
                found.extend(lookup.citations)
            else:
                errors.append(f"{name}: {lookup.error}")

        kept, unattributable, over = clean_citations(found)

        if attempted and len(errors) == attempted:
            # EVERY source that could look, failed. So nothing was looked up,
            # and that is an ERROR rather than an empty result —
            # `was_answered` and `found_nothing` are separate questions
            # precisely here. Reporting a total outage as "we looked and found
            # nothing" would be a claim about the world made from a claim
            # about the network.
            #
            # Keyed on what was ATTEMPTED rather than on a literal 3: with no
            # RSS feeds configured only two sources ever run, and a hardcoded
            # count reported a full outage as a clean empty answer.
            return ResearchAnswer(question=text, error="; ".join(errors))

        return ResearchAnswer(
            question=text,
            citations=kept,
            not_found=_not_found(kept, errors),
            dropped_unattributable=unattributable,
            dropped_over_cap=over,
        )


def _not_found(kept: Iterable[Citation], errors: Sequence[str]) -> str:
    """The honest account of what was missed, in the negative field.

    Names the sources that failed even when others answered, because "three
    quotes from Wikipedia" and "three quotes from Wikipedia, EDGAR unreachable"
    are different statements about how well the chain is sourced — the same
    reason `dropped_unattributable` is counted rather than swallowed.
    """
    parts: list[str] = []
    if not list(kept):
        parts.append(
            "No published source among SEC filings, the configured news feeds "
            "or Wikipedia carried a sentence matching this. Open-web search is "
            "not available here, so this is not evidence that nothing exists."
        )
    if errors:
        parts.append("Sources that could not be reached — " + "; ".join(errors) + ".")
    return " ".join(parts)
