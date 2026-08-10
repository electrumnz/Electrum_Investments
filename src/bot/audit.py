"""Append-only audit log of every decision and trade.

Writes JSONL so it's grep-friendly. One file per UTC date.

## Reading it back

The log is the only record of *why* the bot did or did not act. The journal
says a trade happened; this says what the model proposed, what the risk gate
ruled and on which grounds, and what actually reached the broker. That trail is
what the dashboard's Decisions page renders, and it is the only place a
rejection is visible at all — a proposal the gate refused never becomes a trade,
so it exists nowhere else.

Reading is deliberately tolerant. This is an append-only file written by a
long-running process, so the last line can be a partial write, and a file from
an older build can carry fields this one has never heard of. A reader that
raised on either would take the dashboard down to protect nothing. Unparseable
lines are counted and reported rather than silently skipped, because a log that
quietly drops records is worse than one that admits it lost some.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Decision


class AuditLog:
    def __init__(self, base_dir: Path = Path("audit")) -> None:
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        """Where the dated files live.

        Public because the query index in `insight.py` derives itself from
        these files and must point at the same directory this instance writes
        to — including when a test redirects it to a temp path.
        """
        return self._base

    def _today_path(self) -> Path:
        return self._base / f"{datetime.now(UTC).date().isoformat()}.jsonl"

    def record(self, decision: Decision) -> None:
        line = decision.model_dump_json()
        with self._today_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def record_event(self, kind: str, payload: dict[str, object]) -> None:
        """Log arbitrary events (startup, shutdown, errors) alongside decisions."""
        line = json.dumps(
            {
                "kind": kind,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
        )
        with self._today_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ----------------------------------------------------------------- reading

    def files(self) -> list[Path]:
        """Log files, newest date first. Names are ISO dates, so this sorts."""
        if not self._base.exists():
            return []
        return sorted(self._base.glob("*.jsonl"), reverse=True)

    def read(self, *, limit: int = 200, days: int = 14) -> AuditView:
        """Most recent entries first, newest file first.

        Stops as soon as `limit` decisions have been collected, so a long
        history costs nothing: the files are dated, and the newest is read
        first.
        """
        view = AuditView()
        for path in self.files()[:days]:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                view.unreadable_files.append(f"{path.name}: {exc}")
                continue

            # Reversed: within a file, later lines are newer.
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if not line:
                    continue
                entry = _parse(line)
                if entry is None:
                    view.malformed += 1
                    continue
                if isinstance(entry, DecisionEntry):
                    if len(view.decisions) < limit:
                        view.decisions.append(entry)
                else:
                    if len(view.events) < limit:
                        view.events.append(entry)

            if len(view.decisions) >= limit:
                break

        return view


@dataclass(frozen=True)
class DecisionEntry:
    """One pass of the loop: what was proposed, ruled and executed."""

    timestamp: datetime
    decision: Decision

    @property
    def approved(self) -> int:
        return sum(1 for v in self.decision.verdicts if v.approved)

    @property
    def rejected(self) -> int:
        return sum(1 for v in self.decision.verdicts if not v.approved)

    @property
    def acted(self) -> bool:
        """Whether an order actually filled this cycle.

        Checks `accepted`, not merely that a result exists. A broker refusal is
        recorded as an `OrderResult` too, and treating its presence as success
        would label the one cycle where money did not move as the cycle where
        it did.
        """
        return any(r.accepted for r in self.decision.executed)

    @property
    def broker_refused(self) -> bool:
        """The gate said yes and the broker said no. A distinct outcome."""
        return bool(self.decision.executed) and not self.acted

    @property
    def outcome(self) -> str:
        """One word for the cycle, for scanning a long list.

        "held" is the common case and is deliberately not styled as a failure:
        doing nothing is a valid, frequently correct output.
        """
        if not self.decision.proposals:
            return "held"
        if self.acted:
            return "executed"
        if self.broker_refused:
            return "refused"
        if self.approved:
            return "approved"
        return "rejected"

    def verdict_for(self, index: int) -> Any:
        """Verdict paired with proposal `index`, or None if they do not line up.

        The loop skips a proposal whose symbol has no tick, so the two lists
        can differ in length. Pairing by position and admitting when it cannot
        be done beats rendering one proposal's reasons against another.
        """
        verdicts = self.decision.verdicts
        if len(verdicts) != len(self.decision.proposals):
            return None
        return verdicts[index] if index < len(verdicts) else None


@dataclass(frozen=True)
class EventEntry:
    kind: str
    timestamp: datetime
    payload: dict[str, Any]


@dataclass
class AuditView:
    """Everything read, plus an honest account of what could not be."""

    decisions: list[DecisionEntry] = field(default_factory=list)
    events: list[EventEntry] = field(default_factory=list)
    malformed: int = 0
    unreadable_files: list[str] = field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        """Say the record is incomplete rather than imply it is empty."""
        return self.malformed > 0 or bool(self.unreadable_files)


def _parse(line: str) -> DecisionEntry | EventEntry | None:
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    # Event records carry "kind"; decision records do not.
    if "kind" in payload:
        stamp = _timestamp(payload.get("timestamp"))
        if stamp is None:
            return None
        raw_payload = payload.get("payload")
        return EventEntry(
            kind=str(payload["kind"]),
            timestamp=stamp,
            payload=raw_payload if isinstance(raw_payload, dict) else {},
        )

    try:
        decision = Decision.model_validate(payload)
    except Exception:
        # A record written by an older build with a field that has since
        # changed shape. Counted as malformed rather than crashing the page.
        return None
    return DecisionEntry(timestamp=decision.timestamp, decision=decision)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
