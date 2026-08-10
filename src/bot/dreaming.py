"""The dreamer: second-order investment hypotheses, thought about in public.

The decision loop watches six symbols and four strategies and is deliberately
narrow. This is the other thing: an agent that looks two hops away from what
anyone asked about, records the chain link by link, attacks it, and reaches a
verdict that is never a trade.

The worked example the module is shaped around, and the shape every good dream
has:

    Cicadas emerge on fixed multi-year broods. In a year when two of the three
    largest sesame-producing countries fall inside overlapping brood ranges,
    their harvests are damaged in the same season. Indonesia has no periodical
    cicadas, so Indonesian sesame becomes the marginal supplier into a shortage
    it did not experience.

What makes that worth recording is not the conclusion. It is that every link is
a separate physical claim which can be checked on its own, and any one of them
breaking kills the whole chain. That is why `Hop` is a first-class type here and
why `checked` and `source` sit on it: a chain whose hops cannot be attacked
individually is a story rather than a hypothesis.

## Why this cannot become an order path

`RiskGate.evaluate` vets proposals. The dreamer never makes one, and the
guarantee is structural rather than a matter of discipline:

**`Dream` does not carry the fields an order needs.** No quantity, no entry
price, no stop-loss price, no side. `OrderProposal` requires all of them and
`stop_loss_price` is mandatory and validated, so there is no code path that
turns one of these into the other without a person writing new fields and new
validation. `tests/test_dreaming.py` asserts that overlap stays empty.

That is on purpose and it is the whole safety argument. A speculative-idea
generator wired into an execution path is the Alpha Arena failure with extra
steps: six frontier models traded real money on confident prose and every US
flagship finished underwater. Confidence is exactly what this module produces
most of, so it is kept the furthest from the account of anything in the repo.

`instruments` names what a dream is *about* — a commodity, a region, a listed
proxy. It is a subject, not an instruction, and it is deliberately free text so
that it cannot be mistaken for a symbol the bot trades.

## This is not Anthropic's "Dreaming", and the difference matters

Anthropic shipped a feature called Dreaming as a research preview in May 2026:
a memory *consolidation* pass that reads an agent's own session transcripts and
folds corrections, preferences and recurring patterns back into its memory files
between sessions. Same word, different machine. That one is about an agent
learning from its own mistakes; this one is about generating second-order
hypotheses.

The overlap is real and it is the dangerous part, so it is worth being explicit.
"Learn from what happened last time" applied to a trading account means learning
from profit and loss, and this repository forbids that deliberately: forty
trades is noise, a model shown three losses will confidently change approach,
and that is the Alpha Arena failure exactly. `souls/grogu.md` says it in the
character file and `DreamLedger` is the shape learning is allowed to take here.

**Consolidation over reasoning quality, never over returns.** `DreamLedger`
counts how often the dreamer sources a hop, how often it drops a chain, how
often it names a trigger. Those are facts about the thinking, they are true
regardless of how any trade went, and there is no sample of outcomes to overfit
to. They reach the operator on the Dreaming page and stop there, exactly as
`metrics.py` reaches the Analytics page and stops there. The loop that closes is
journal to figures to a human to a commit, at human speed, with an audit trail.

## Why the store is its own file

`data/dreams.db`, not `data/journal.db`. The journal is the only irreplaceable
file on the box and `deploy/backup-journal.sh` snapshots it hourly; losing every
dream ever recorded costs some speculative notes and nothing else. Keeping them
apart also means no query can accidentally read a hypothesis as a position.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_DREAMS_PATH = Path("data/dreams.db")

# Free-form prose is trimmed rather than rejected, exactly as `claude.propose`
# treats a rationale: nothing downstream parses it, so losing the tail of a
# sentence costs a reader some context where a rejected response would cost the
# whole dream. The same leniency is NOT extended to anything structural.
TEXT_MAX_CHARS = 900


class DreamStage(StrEnum):
    """Where a mini-project has got to.

    The order matters: `iterate` can loop back to `explore`, and only `verdict`
    is terminal. A dream that never leaves `explore` is a dream that is still
    running, which is a normal state and is rendered as one.
    """

    SEED = "seed"
    EXPLORE = "explore"
    ITERATE = "iterate"
    VERDICT = "verdict"


class DreamVerdict(StrEnum):
    """How a mini-project ended. None of these is an instruction.

    `DROP` is the one worth defending. A dream that fell apart on inspection is
    a good outcome and recording *which hop broke* is what stops the same idea
    arriving again next month wearing a different headline.
    """

    KEEP = "keep"
    PARK = "park"
    DROP = "drop"


class Verification(StrEnum):
    """How much of a chain rests on something somebody actually looked at.

    Deliberately not a percentage and not a five-point scale. Confidence in a
    causal chain is the minimum across its links, and a number invites an
    average. This is a statement about evidence, which can be audited, rather
    than about belief, which cannot.
    """

    UNVERIFIED = "unverified"  # at least one hop is an assumption
    PARTIAL = "partial"  # some hops sourced, the gaps are named
    SOURCED = "sourced"  # every hop names where it came from


@dataclass(frozen=True)
class Hop:
    """One link in a causal chain, checkable on its own.

    `source` is empty when `checked` is False, and the pair is what lets the
    Dreaming page mark a chain honestly rather than presenting six confident
    sentences of which two were invented.
    """

    claim: str
    checked: bool = False
    source: str = ""

    def to_row(self) -> dict[str, Any]:
        return {"claim": self.claim, "checked": self.checked, "source": self.source}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Hop:
        return cls(
            claim=str(row.get("claim", "")),
            checked=bool(row.get("checked", False)),
            source=str(row.get("source", "")),
        )


@dataclass(frozen=True)
class Thought:
    """One entry in the thoughts stream, at a point in time.

    Append-only, like the audit log and for the same reason: the interesting
    thing about a dream is usually the step where it changed its mind, and a
    store that overwrote its working would throw exactly that away.
    """

    stage: DreamStage
    text: str
    at: datetime
    # WHO thought it. Empty today, because there is one dreamer.
    #
    # It exists now rather than later because the intended direction is several
    # dreamers working a topic independently and then arguing it out, and a
    # debate whose transcript cannot say who said what is not a transcript. The
    # field is optional with a default for the same reason every field on
    # `Decision` is: this store is append-only and never migrated, so a reader
    # that rejected yesterday's rows would throw away the history it exists to
    # keep. Adding it now costs one line; adding it after a year of dreams costs
    # a migration and the attribution is unrecoverable anyway.
    by: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "text": self.text,
            "at": self.at.isoformat(),
            "by": self.by,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Thought:
        return cls(
            stage=_stage(row.get("stage")),
            text=str(row.get("text", "")),
            at=_dt(row.get("at")),
            by=str(row.get("by", "")),
        )


@dataclass
class Dream:
    """One mini-project.

    Carries no quantity, no entry, no stop and no side, and must not gain any.
    See the module docstring: that absence is the reason this module is allowed
    to exist alongside a live order path.
    """

    title: str
    seed: str
    stage: DreamStage = DreamStage.SEED
    chain: list[Hop] = field(default_factory=list)
    thoughts: list[Thought] = field(default_factory=list)
    verdict: DreamVerdict | None = None
    # The hop the whole thing rests on. Named explicitly because a reader who
    # only has time for one sentence should be given the one that could kill it.
    weakest_hop: str = ""
    # What has to happen for a `keep` to become interesting, or a `park` to wake
    # up. A watch with no named trigger is not a plan, which is the same rule
    # the Decisions page applies to the decision loop's own watches.
    trigger: str = ""
    instruments: list[str] = field(default_factory=list)
    origin: str = ""
    id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def verification(self) -> Verification:
        """Derived from the chain, never set by the agent.

        A model asked to rate its own sourcing will rate it generously. This
        counts `checked` flags instead, so the badge on the page is arithmetic
        over what the dream actually recorded.
        """
        if not self.chain:
            return Verification.UNVERIFIED
        checked = sum(1 for hop in self.chain if hop.checked)
        if checked == len(self.chain):
            return Verification.SOURCED
        if checked == 0:
            return Verification.UNVERIFIED
        return Verification.PARTIAL

    @property
    def unverified_hops(self) -> list[Hop]:
        return [hop for hop in self.chain if not hop.checked]

    @property
    def is_open(self) -> bool:
        return self.stage is not DreamStage.VERDICT

    def add_thought(
        self,
        stage: DreamStage,
        text: str,
        *,
        at: datetime | None = None,
        by: str = "",
    ) -> None:
        self.thoughts.append(
            Thought(stage=stage, text=_trim(text), at=at or datetime.now(UTC), by=by)
        )
        self.stage = stage
        self.updated_at = at or datetime.now(UTC)


def _trim(text: str) -> str:
    """Prose truncates. Nothing structural goes through here.

    Same split as the decision loop: a rationale that overran a cap once killed
    a live cycle and restarted straight into the same failure, so free text is
    trimmed rather than rejected. A number is never trimmed, because a truncated
    number is a different number.
    """
    clean = text.strip()
    if len(clean) <= TEXT_MAX_CHARS:
        return clean
    return clean[: TEXT_MAX_CHARS - 1].rstrip() + "…"


def _stage(value: object) -> DreamStage:
    try:
        return DreamStage(str(value))
    except ValueError:
        # An unreadable stage is a display problem, not a reason to lose the
        # row. Same tolerance as the audit reader, which counts what it could
        # not parse rather than dropping it silently.
        return DreamStage.SEED


def _dt(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


SCHEMA = """
CREATE TABLE IF NOT EXISTS dreams (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  title        TEXT NOT NULL,
  seed         TEXT NOT NULL,
  stage        TEXT NOT NULL,
  verdict      TEXT,
  weakest_hop  TEXT NOT NULL DEFAULT '',
  trigger_note TEXT NOT NULL DEFAULT '',
  origin       TEXT NOT NULL DEFAULT '',
  chain        TEXT NOT NULL DEFAULT '[]',
  thoughts     TEXT NOT NULL DEFAULT '[]',
  instruments  TEXT NOT NULL DEFAULT '[]',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
"""


class DreamStore:
    """SQLite store for dreams. Its own file, not the journal.

    Deliberately forgiving on read and strict on write, in the same shape as
    `audit.py`: a row that will not parse is skipped and counted rather than
    raising, because the page that renders these is one an operator opens to
    look at everything else too.
    """

    def __init__(self, path: Path = DEFAULT_DREAMS_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, dream: Dream) -> int:
        """Insert or update. Returns the row id."""
        payload = (
            dream.title,
            dream.seed,
            str(dream.stage),
            str(dream.verdict) if dream.verdict else None,
            dream.weakest_hop,
            dream.trigger,
            dream.origin,
            json.dumps([h.to_row() for h in dream.chain]),
            json.dumps([t.to_row() for t in dream.thoughts]),
            json.dumps(dream.instruments),
            dream.created_at.isoformat(),
            dream.updated_at.isoformat(),
        )
        with self._connect() as conn:
            if dream.id is None:
                cursor = conn.execute(
                    "INSERT INTO dreams (title, seed, stage, verdict, weakest_hop,"
                    " trigger_note, origin, chain, thoughts, instruments, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    payload,
                )
                dream.id = int(cursor.lastrowid or 0)
            else:
                conn.execute(
                    "UPDATE dreams SET title=?, seed=?, stage=?, verdict=?,"
                    " weakest_hop=?, trigger_note=?, origin=?, chain=?, thoughts=?,"
                    " instruments=?, created_at=?, updated_at=? WHERE id=?",
                    (*payload, dream.id),
                )
        return dream.id or 0

    def recent(self, limit: int = 30) -> list[Dream]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dreams ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out: list[Dream] = []
        for row in rows:
            parsed = self._to_dream(row)
            if parsed is not None:
                out.append(parsed)
        return out

    def get(self, dream_id: int) -> Dream | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dreams WHERE id=?", (dream_id,)).fetchone()
        return self._to_dream(row) if row else None

    @staticmethod
    def _to_dream(row: sqlite3.Row) -> Dream | None:
        try:
            chain = [Hop.from_row(h) for h in json.loads(row["chain"] or "[]")]
            thoughts = [Thought.from_row(t) for t in json.loads(row["thoughts"] or "[]")]
            instruments = [str(i) for i in json.loads(row["instruments"] or "[]")]
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            # Counted rather than raised, for the reason on the class. One bad
            # row must not take the page down.
            log.warning("dream_unreadable", dream_id=row["id"], error=str(exc))
            return None

        verdict_raw = row["verdict"]
        try:
            verdict = DreamVerdict(verdict_raw) if verdict_raw else None
        except ValueError:
            verdict = None

        return Dream(
            id=int(row["id"]),
            title=str(row["title"]),
            seed=str(row["seed"]),
            stage=_stage(row["stage"]),
            verdict=verdict,
            weakest_hop=str(row["weakest_hop"] or ""),
            trigger=str(row["trigger_note"] or ""),
            origin=str(row["origin"] or ""),
            chain=chain,
            thoughts=thoughts,
            instruments=instruments,
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )


# Below this many resolved dreams, every rate here is noise. Same reasoning and
# roughly the same threshold as `PerformanceSummary.sample_is_thin`: a drop rate
# computed over four chains says nothing, and a figure presented without its
# sample count gets believed anyway.
THIN_LEDGER_THRESHOLD = 12


@dataclass(frozen=True)
class DreamLedger:
    """What the dreamer has learned about its own thinking.

    This is the consolidation pass, and it is deliberately narrow. It counts
    properties of the REASONING — was a hop sourced, was a chain attacked, was a
    trigger named — and never touches what any idea would have earned. See the
    module docstring: consolidation over reasoning quality is safe because the
    facts are true regardless of how a trade went, and there is no outcome
    sample to overfit to.

    It reaches the operator through the Dreaming page and stops there. Nothing
    here is fed back to the model, for the same reason `metrics.py` is not.

    Every rate is `None` rather than zero when there is nothing to divide by.
    A sourcing rate of "0%" on an empty store reads as a damning result; "n/a"
    reads as the absence of evidence it actually is.
    """

    dreams: int
    resolved: int
    sourcing_rate: float | None
    drop_rate: float | None
    median_hops: float | None
    untriggered_keeps: int
    unattacked: int

    @property
    def sample_is_thin(self) -> bool:
        return self.resolved < THIN_LEDGER_THRESHOLD

    @classmethod
    def of(cls, dreams: list[Dream]) -> DreamLedger:
        hops = [hop for d in dreams for hop in d.chain]
        resolved = [d for d in dreams if d.verdict is not None]
        lengths = sorted(len(d.chain) for d in dreams if d.chain)

        median: float | None = None
        if lengths:
            middle = len(lengths) // 2
            median = (
                float(lengths[middle])
                if len(lengths) % 2
                else (lengths[middle - 1] + lengths[middle]) / 2
            )

        return cls(
            dreams=len(dreams),
            resolved=len(resolved),
            sourcing_rate=(
                sum(1 for h in hops if h.checked) / len(hops) * 100 if hops else None
            ),
            # A dreamer that never drops anything is not attacking its chains,
            # which is the failure this number exists to make visible. High is
            # healthy here, which is why it is labelled rather than left to be
            # read as a defect rate.
            drop_rate=(
                sum(1 for d in resolved if d.verdict is DreamVerdict.DROP)
                / len(resolved)
                * 100
                if resolved
                else None
            ),
            median_hops=median,
            untriggered_keeps=sum(
                1 for d in dreams if d.verdict is DreamVerdict.KEEP and not d.trigger
            ),
            unattacked=sum(1 for d in dreams if d.chain and not d.weakest_hop),
        )


@dataclass(frozen=True)
class DreamSummary:
    """Counts for the page header. Pure arithmetic over what is stored."""

    total: int
    open_dreams: int
    kept: int
    parked: int
    dropped: int
    unverified: int

    @classmethod
    def of(cls, dreams: list[Dream]) -> DreamSummary:
        return cls(
            total=len(dreams),
            open_dreams=sum(1 for d in dreams if d.is_open),
            kept=sum(1 for d in dreams if d.verdict is DreamVerdict.KEEP),
            parked=sum(1 for d in dreams if d.verdict is DreamVerdict.PARK),
            dropped=sum(1 for d in dreams if d.verdict is DreamVerdict.DROP),
            unverified=sum(
                1 for d in dreams if d.verification is Verification.UNVERIFIED
            ),
        )
