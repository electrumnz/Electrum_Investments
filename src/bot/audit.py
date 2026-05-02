"""Append-only audit log of every decision and trade.

Writes JSONL so it's grep-friendly. One file per UTC date.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .models import Decision


class AuditLog:
    def __init__(self, base_dir: Path = Path("audit")) -> None:
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)

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
