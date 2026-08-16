"""A third Hermes instance that can reach the web, and cannot reach anything else.

`TODO.md` item 26. The dreamer reasons about cicada broods and smelter
restarts, and it does that with no way to look anything up — so every hop it
writes is either reference knowledge it already had or an invention, and
`Hop.checked` exists because some of those sentences were invented. A
researcher closes that gap by GOING AND LOOKING.

**Web access is the thing `docs/HANDOFF.md` deferred rather than refused**, and
it records two rules that hold whichever shape it takes. Both are load-bearing
here and both are structural rather than remembered:

- **Nothing web-derived may become a gating input.** `RiskGate` has to stay
  deterministic and must not fail open on a network call. Nothing in this
  module imports `risk`, `broker`, `journal`, `reconcile`, `mcp_server` or
  `grants`, and `tests/test_research.py` parses this file's AST to prove it —
  the same shape as the test that pins `confer.TraderPowers` away from the
  broker.
- **The model reads rendered, attributable text, never raw pages.** That is
  the `indicators.py` rule arriving from a new direction: a model handed a page
  and asked what it means will produce a meaning, state it confidently, and be
  believed.

## Quotes and URLs, never conclusions

This is the requirement that makes the feature worth having rather than
dangerous, and it is enforced by the SHAPE of what comes back.

**A summary launders provenance.** "Indonesian sesame exports are under
pressure" is a sentence with no author, no date and nothing to check; the
paragraph it was distilled from had all three. Once the distillation happens
the reader cannot tell which part somebody published and which part the model
supplied, and a dreamer handed that sentence will treat it as reference data
and mark the hop checked.

So `Citation` carries a QUOTE, a URL and when it was retrieved, and it carries
no field a conclusion could live in — no summary, no implication, no
significance, no relevance. `ResearchAnswer` is a list of those plus an honest
account of what was NOT found. The test asserting the field overlap with
`OrderProposal` and with `Dream` is empty is the same guarantee
`test_a_dream_cannot_describe_an_order` makes one layer up: a citation cannot
become an order, and it cannot become a permission either.

**The researcher is not a fourth voice with opinions.** It answers a question
with other people's words. What those words MEAN is the dreamer's job, in the
dreamer's own chain, where every hop is attackable one at a time.

## Why a third instance rather than a tool on an existing one

Hermes loads one soul per instance and one MCP registry per instance, and
`deploy/run-dream.sh` already exists for exactly that reason. The argument
repeats with the direction reversed:

- The account agent's Hermes registers this repo's MCP server, which exposes
  `place_order`. **A web-reading agent must not sit in that process**, because
  a page is untrusted text and that process can place orders.
- The dreamer's Hermes has no MCP server and is the one that would most like
  the web — but giving it the web makes the dreamer's own instance a place
  where a fetched page and a speculative chain meet with nothing between them.

So `deploy/run-research.sh` is a third home: the `web` toolset enabled, **no
`mcp_servers` block at all**, and therefore no route to the broker, to the
dream store or to the journal. The quarantine is a property of the process
rather than of a sentence in a soul file.

**Its config uses an ALLOWLIST (`toolsets:`) rather than the denylist the other
two use**, which is the opposite of `deploy/hermes-config.yaml` and is
deliberate. A denylist admits whatever the next Hermes release adds; on the
other two instances the cost of that is a tool that cannot work on a headless
box, and here the cost is a web-reading process quietly gaining a capability
nobody chose. Getting an allowlist wrong yields an agent with almost no tools,
which is visible and safe. Getting a denylist wrong yields an agent with more
reach than intended, which is invisible.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .hermes import DEFAULT_USER, HermesRunner

# The wrapper, and it is a DIFFERENT file from `run-chat.sh` and
# `run-dream.sh` for the reason those two are different from each other: the
# sudoers rule names a path with no arguments, so the only way to select an
# instance is to name a different file.
DEFAULT_BINARY = Path("/opt/mudhorn/deploy/run-research.sh")

# ---------------------------------------------------------------- the caps
#
# Its own, and separate from the conference's, because the two are bounded
# against different things. `confer`'s caps bound a NEGOTIATION — six turns,
# two dreams a day — and exist so two agents cannot re-litigate an unchanged
# idea forever. These bound a FETCHER, whose failure mode is different: an
# unbounded researcher spends money and somebody else's rate limit, and the
# thing it is spending is not visible from the dream it was asked about.

#: Questions a single run may put. The dreamer produces one step a day, so a
#: handful of look-ups is the whole appetite; anything larger is a crawler.
MAX_QUESTIONS_PER_RUN = 3

#: Citations kept per question. A model asked for evidence will keep going,
#: and twenty quotes is a document rather than an answer. Excess is DROPPED
#: and the drop is counted, never silently truncated — see `dropped_citations`.
MAX_CITATIONS_PER_QUESTION = 5

#: How much of somebody else's page may be quoted. Long enough to carry the
#: claim with its qualifiers, short enough that it stays a quote rather than
#: becoming the article. A quote longer than this is TRIMMED with an ellipsis
#: — prose truncates, the same rule `RATIONALE_MAX_CHARS` follows — because
#: losing the tail of a sentence costs a reader some context, where rejecting
#: the citation costs the whole look-up.
MAX_QUOTE_CHARS = 600

#: One turn's wall clock. Longer than the chat bridge's because a web search
#: and two extractions are several round trips, and short enough that a stuck
#: turn does not hold the dream timer.
DEFAULT_TIMEOUT_SECONDS = 300.0


def _one_line(text: str) -> str:
    """Collapse every kind of whitespace, including newlines.

    **`.strip()` is not enough and that is a bug this repository has already
    paid for.** Everything here is rendered into a markdown document a model
    reads, so a value carrying the document's own structural characters can
    restructure it — a quote containing a newline can close its bullet and open
    its own `## Gate verdicts (previous cycle)` section. `marketaux._parse` had
    exactly this hole and `xfeed` was safe only by accident.

    It matters MORE here than there. A headline comes from a news API with a
    schema; a quote comes from an arbitrary page that anybody can publish, and
    the whole point of this module is to read pages nobody vetted.
    """
    return " ".join(text.split())


@dataclass(frozen=True)
class Citation:
    """Something somebody else wrote, quoted, with where and when it came from.

    **There is deliberately nowhere in this type to put a conclusion.** No
    summary, no implication, no significance, no "what this means for". A
    summary launders provenance: the distilled sentence has no author and no
    date, and a reader cannot tell which half somebody published and which half
    the model supplied. The paragraph it came from had both.

    It also carries no `symbol`, no `symbols` and no `asset_class_key`. A
    `Dream` carries those two because they are a PERMISSION the operator's
    machinery grants deliberately; a citation is a thing found on the internet,
    and a route from a fetched page to a tradeable-symbol claim is the one
    connection this module must not make. `tests/test_research.py` pins the
    field overlap with both `OrderProposal` and `Dream` at empty.
    """

    quote: str
    url: str
    retrieved_at: datetime
    publisher: str = ""

    @property
    def is_attributable(self) -> bool:
        """Whether this can be traced back to something a person can open.

        A quote with no URL is a sentence the model produced, whatever it says
        about where it came from — which is the entire failure mode this type
        exists to prevent, so it is a question with a name rather than a filter
        applied silently. `ResearchAnswer` drops these and counts them.
        """
        return bool(self.quote.strip()) and _looks_like_a_url(self.url)


_URL = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def _looks_like_a_url(url: str) -> bool:
    """A syntactic check, and it does not claim to be more than that.

    Nothing here fetches the URL to confirm it resolves — this module makes no
    network call of its own, which is what keeps it out of every gating path.
    So this establishes that the field holds something a reader can paste into
    a browser, and says nothing about whether the page says what the quote
    claims. The operator settling an observation is what does that.
    """
    return bool(_URL.match(url.strip()))


@dataclass(frozen=True)
class ResearchAnswer:
    """What one question came back with: other people's words, and the gaps.

    `found_nothing` is a separate question from "is the list empty", the same
    way `has_cycles`, `can_grade_anything` and first-visit are kept apart from
    empty everywhere else here. A question that was put and came back with
    nothing, and a question that was never put because the researcher was
    unavailable, are opposite findings, and only one of them says anything
    about the world.
    """

    question: str
    citations: list[Citation] = field(default_factory=list)
    #: What the researcher could not find, in its own words. The one prose
    #: field, and it is deliberately the NEGATIVE one: a sentence about what is
    #: missing cannot be mistaken for a finding, where a sentence about what
    #: was found would be exactly the laundered conclusion this type refuses.
    not_found: str = ""
    #: Citations discarded because they carried no usable URL, and citations
    #: discarded because the cap was reached. Counted rather than dropped in
    #: silence: "five quotes" and "five quotes out of nine, four unusable" are
    #: different statements about how well the look-up went.
    dropped_unattributable: int = 0
    dropped_over_cap: int = 0
    #: Set when the turn itself failed — Hermes absent, a timeout, a non-zero
    #: exit. Distinct from an empty citation list for the reason above.
    error: str = ""

    @property
    def was_answered(self) -> bool:
        """Did a researcher actually answer, whatever it found?"""
        return not self.error

    @property
    def found_nothing(self) -> bool:
        """Answered, and came back with no usable citation.

        A real and respectable outcome. The soul says so and this says so
        again, because the tempting repair for an empty answer is a plausible
        one, and a fabricated quote is worse here than anywhere else in the
        repository — it arrives wearing a URL.
        """
        return self.was_answered and not self.citations

    def render(self) -> str:
        """The answer as a person or a dreamer reads it. Quotes and URLs only.

        Every line is somebody else's sentence with its source beside it, and
        the block says out loud that nothing in it has been checked. That
        caveat is not decoration: a fetched page is untrusted text, and an
        unqualified quotation in a prompt reads as established fact for exactly
        the reason `Hop.checked` and the `Verification` badge exist.
        """
        if self.error:
            return (
                f'Researcher could not answer "{_one_line(self.question)}": '
                f"{_one_line(self.error)}. Nothing was looked up, which is NOT "
                "a finding that there is nothing to find."
            )
        head = f'Asked: "{_one_line(self.question)}"'
        if not self.citations:
            gap = _one_line(self.not_found) or "No reason given."
            return (
                f"{head}\nNothing usable came back. {gap}\n"
                "An empty answer is an answer. It is not evidence of absence, "
                "and it is not a licence to supply the sentence yourself."
            )
        lines = [head, "Quoted, unverified, from pages nobody here has vetted:"]
        for c in self.citations:
            who = f"{_one_line(c.publisher)}, " if c.publisher.strip() else ""
            lines.append(
                f'- "{_one_line(c.quote)}" — {who}{c.url} '
                f"(retrieved {c.retrieved_at:%Y-%m-%d %H:%M} UTC)"
            )
        if self.not_found.strip():
            lines.append(f"Not found: {_one_line(self.not_found)}")
        for count, why in (
            (self.dropped_unattributable, "carried no usable URL"),
            (self.dropped_over_cap, f"exceeded the {MAX_CITATIONS_PER_QUESTION} cap"),
        ):
            if count:
                lines.append(f"({count} further item(s) dropped: {why}.)")
        return "\n".join(lines)


def clean_citations(
    raw: Iterable[Citation],
) -> tuple[list[Citation], int, int]:
    """Normalise, drop the unattributable, apply the cap. Returns the counts.

    Three jobs in one pass because they have to happen in one order: whitespace
    is collapsed FIRST, so a quote that is only newlines is recognised as
    empty; the unattributable are dropped SECOND, so the cap is spent on
    citations a reader can actually open; and the cap applies LAST.

    Applying the cap first would let five unusable items crowd out five usable
    ones and report a clean five-of-five, which is the plausible-wrong-figure
    shape arriving through an ordering nobody is looking at.
    """
    kept: list[Citation] = []
    unattributable = 0
    for item in raw:
        tidy = Citation(
            quote=_trim(_one_line(item.quote)),
            url=item.url.strip(),
            retrieved_at=item.retrieved_at,
            publisher=_one_line(item.publisher),
        )
        if not tidy.is_attributable:
            unattributable += 1
            continue
        kept.append(tidy)
    over = max(0, len(kept) - MAX_CITATIONS_PER_QUESTION)
    return kept[:MAX_CITATIONS_PER_QUESTION], unattributable, over


def _trim(quote: str) -> str:
    """Prose truncates; see `RATIONALE_MAX_CHARS` for the rule and its limit.

    A quote is prose and nothing downstream parses it, so losing the tail costs
    a reader some context. The ellipsis is what keeps that honest — a silently
    shortened quotation is a misquotation, and this one has a URL beside it
    inviting somebody to go and check.
    """
    if len(quote) <= MAX_QUOTE_CHARS:
        return quote
    return quote[: MAX_QUOTE_CHARS - 1].rstrip() + "…"


def questions_for_run(questions: Sequence[str]) -> tuple[list[str], int]:
    """The questions this run will put, and how many were left out.

    Returns the dropped COUNT rather than silently slicing, because a run that
    answered three of eight and a run that was asked three are different facts
    — the no-silent-caps rule, which this repository states as: if a workflow
    bounds coverage, say what was dropped, or the record reads as complete.
    """
    asked = [q for q in (q.strip() for q in questions) if q]
    return asked[:MAX_QUESTIONS_PER_RUN], max(0, len(asked) - MAX_QUESTIONS_PER_RUN)


def render_for_dreamer(answers: Sequence[ResearchAnswer]) -> str:
    """The block the dreamer is shown, or an empty string when there is nothing.

    Empty rather than a heading with nothing under it: a section announcing
    zero findings trains a reader — and a model — to skip the section, which is
    the argument the Dreaming page's "Waiting on you" card is absent rather
    than empty for.

    **What this block must never say is what the quotes MEAN.** It hands over
    sentences other people published so the dreamer can build a hop on one and
    mark it checked with a URL behind it. The meaning is the dreamer's, in the
    dreamer's own chain, where every link is attackable one at a time.
    """
    return render_lookups(a.render() for a in answers)


#: The heading and the caveat that must travel with every quotation.
#:
#: **The caveat is not decoration.** An unqualified quotation in a prompt reads
#: as established fact, for exactly the reason `Hop.checked` and the
#: `Verification` badge exist — and these come from pages nobody here vetted,
#: which is a weaker provenance than anything else in the dreamer's context.
CITATION_PREAMBLE = (
    "## What the researcher found\n"
    "Quotations from pages, with their sources. Nobody here has checked that "
    "any of them is true, or that the page still says it. A quote is a thing "
    "to build a hop on and cite; it is not a hop, and it is not a reason to "
    "name a symbol.\n\n"
)


def render_lookups(blocks: Iterable[str]) -> str:
    """The citation section, from blocks already rendered.

    Separate from `render_for_dreamer` because the answers reach the prompt one
    RUN LATER than they were fetched — stored as messages on the dream, read
    back on the next step — so by then they are text rather than
    `ResearchAnswer`s. One preamble either way, because two copies of the
    caveat is two places for one of them to lose it.
    """
    body = "\n\n".join(b for b in blocks if b.strip())
    return CITATION_PREAMBLE + body if body else ""


def utc_now() -> datetime:
    """Injectable in tests, and UTC everywhere, for the reason every other
    stamp here is: an ISO string compared against a `+13:00` stamp sorts wrong,
    which has already cost this repository a live permission bug in
    `grants.py`."""
    return datetime.now(UTC)


# ------------------------------------------------------- asking the researcher
#
# Everything above is pure. Everything below runs a SUBPROCESS — and it is
# still not a network call from here: the fetching happens in another process,
# as another user, in a home with no MCP server. That is what keeps this module
# importable from a render path and out of every gating path.


#: What the researcher is told to return, and the shape it must return it in.
#:
#: **A strict, boring format rather than free prose**, for the reason
#: `model_client` forces a tool call rather than parsing prose: a reply that
#: has to be interpreted is a reply where the interpreting is done by whatever
#: is most convenient, and here the convenient reading of an unsourced sentence
#: is that it came from somewhere.
#:
#: One citation per line, pipe-separated, because a fenced JSON block is a
#: second thing that can be malformed and a quote is exactly the kind of text
#: that carries a quotation mark. A pipe cannot appear in a URL and is rare in
#: prose; where it does appear in a quote it costs that one field, not the
#: parse. Anything unparseable is COUNTED, never guessed at.
ASK_TEMPLATE = """\
Question: {question}

Search the web and answer with QUOTATIONS ONLY.

Return one line per source, in exactly this format, and nothing else:

CITE | <the exact words as published> | <publisher> | <https://full-url>

Rules:
- The quote must be words you actually read on the page at that URL. Do not
  paraphrase, do not summarise, do not join two sentences into one.
- Do not tell me what any of it means, what it implies, or what it is
  evidence for. That is not your job and a conclusion here is worse than
  nothing, because it arrives wearing a URL.
- If you found nothing usable, return exactly one line:
  NOTHING | <one sentence on what you looked for and did not find>
- An empty answer is a real answer. Do not fill the gap with a plausible
  sentence.
"""

_CITE = "CITE"
_NOTHING = "NOTHING"


@dataclass
class Researcher:
    """Puts questions to the third Hermes instance and returns other people's words.

    **Off unless the wrapper is permitted, and it SAYS which.** `available`
    asks whether the file is there, `permitted` asks whether sudo will run it,
    and the gap between those two was a live fault on this box: the dreamer was
    routed to an instance sudo refuses and a surface claimed an isolation that
    did not exist. `bot.hermes.HermesRunner` holds both questions and this
    reads the strict one, so a box without the sudoers rule gets no research
    and an honest `error` rather than a silent empty answer.
    """

    runner: HermesRunner = field(
        default_factory=lambda: HermesRunner(
            binary=DEFAULT_BINARY,
            run_as=DEFAULT_USER,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        )
    )
    now: Callable[[], datetime] = utc_now

    @property
    def enabled(self) -> bool:
        """Whether a question put now would reach anything.

        `permitted` rather than `available`, and that is the whole point of
        there being two: the researcher's wrapper ships on every box and is
        chmod'ed by `bootstrap.sh` whether or not the instance behind it was
        ever created. Reading the weaker question would route questions at a
        sudo that refuses them and report the refusal as a research failure.
        """
        return self.runner.permitted

    def ask(self, question: str) -> ResearchAnswer:
        """One question, one answer. Never raises.

        A failure is an `error` on the answer rather than an exception, because
        the caller is a scheduled job that must not die of a look-up: the same
        reason `fetch_market_ticks` catches broadly. And an `error` is not an
        empty result — `was_answered` and `found_nothing` are separate
        questions for exactly this.
        """
        text = question.strip()
        if not text:
            return ResearchAnswer(question=question, error="empty question")
        if not self.enabled:
            return ResearchAnswer(
                question=text,
                error=(
                    "no researcher instance is permitted here "
                    f"({DEFAULT_BINARY}); see deploy/enable-research.sh"
                ),
            )

        result = self.runner.run(ASK_TEMPLATE.format(question=text))
        if not result.ok:
            return ResearchAnswer(question=text, error=result.error)
        return parse_reply(text, result.text, now=self.now())


def parse_reply(question: str, reply: str, *, now: datetime) -> ResearchAnswer:
    """Turn the researcher's lines into citations, counting what did not parse.

    Tolerant in one direction only. An unrecognised line is DROPPED and
    counted, never salvaged into a citation with a guessed URL — a citation is
    the one thing here that must not be constructed locally, because its whole
    value is that somebody else wrote it and a reader can go and check.

    `retrieved_at` is stamped HERE rather than read from the reply. A model
    asked for a timestamp will produce one, and a fabricated retrieval time on
    a real quote is the kind of plausible wrong figure this repository exists
    to refuse. What we can honestly say is when we asked.
    """
    raw: list[Citation] = []
    not_found = ""
    unparsed = 0

    for line in reply.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        head, _, rest = stripped.partition("|")
        label = head.strip().upper()
        if label == _NOTHING:
            # First one wins. A researcher that says NOTHING twice has said it
            # once as far as a reader is concerned, and concatenating them
            # would build a paragraph out of two attempts at one sentence.
            not_found = not_found or _one_line(rest)
            continue
        if label != _CITE:
            unparsed += 1
            continue
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) < 3:
            unparsed += 1
            continue
        quote, publisher, url = parts[0], parts[1], parts[2]
        raw.append(Citation(quote=quote, url=url, retrieved_at=now, publisher=publisher))

    kept, unattributable, over = clean_citations(raw)
    return ResearchAnswer(
        question=question,
        citations=kept,
        not_found=not_found,
        # An unparseable line and an unattributable one are the same kind of
        # loss to a reader — something came back and could not be used — and
        # they are reported together rather than as a third counter nobody
        # reads. The distinction that matters is kept/dropped, and both sides
        # of it are stated.
        dropped_unattributable=unattributable + unparsed,
        dropped_over_cap=over,
    )
