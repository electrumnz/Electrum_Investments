"""Tests for the decision loop's observability.

The loop only speaks when something happens: a verdict per proposal, a warning
per expiry alert. But standing pat is the common and intended output, so on a
quiet day every one of those is silent — and a healthy bot becomes
indistinguishable from a wedged one in `journalctl`.

`cycle_complete` is the fix, and these tests exist because a heartbeat nobody
verifies is worse than no heartbeat: it is something an operator learns to
trust and then cannot.

No network, no real account: the broker is a `MockBroker` and Claude is stubbed.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import Any

import structlog

import bot.main as main_mod
from bot.audit import AuditLog
from bot.claude_client import CallUsage, ClaudeDecision
from bot.config import Env, load_rules
from bot.journal import Journal


class _StubClaude:
    """Stands in for ClaudeClient. Returns whatever decision the test asked for."""

    def __init__(self, decision: ClaudeDecision) -> None:
        self._decision = decision

    def propose(self, market_context: str) -> tuple[ClaudeDecision, CallUsage]:
        return self._decision, CallUsage(
            input_tokens=2072,
            output_tokens=129,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=0.002717,
        )


def _run_one_cycle(
    monkeypatch, tmp_path, decision: ClaudeDecision
) -> list[MutableMapping[str, Any]]:
    """Run exactly one pass of `cmd_loop` and return the structlog events.

    The loop is a `while True`, so it is stopped the way a person stops it:
    `time.sleep` at the foot of the cycle raises KeyboardInterrupt, which
    `cmd_loop` already handles as a clean exit.
    """

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "journal.db"))
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(main_mod, "ClaudeClient", lambda *a, **k: _StubClaude(decision))

    env = Env(_env_file=None)  # type: ignore[call-arg]
    rules = load_rules()

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(env, rules, execute=False, force_mock=True) == 0
    return logs


def _heartbeat(logs: list[MutableMapping[str, Any]]) -> MutableMapping[str, Any]:
    beats = [e for e in logs if e["event"] == "cycle_complete"]
    assert len(beats) == 1, f"expected exactly one heartbeat, got {len(beats)}"
    return beats[0]


def test_quiet_cycle_still_logs_a_heartbeat(monkeypatch, tmp_path):
    """The case the heartbeat exists for: Claude proposes nothing at all.

    Without it this cycle produces no output whatsoever, which is the whole
    problem — silence has to mean "stopped", not "working normally".
    """
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Thin tape, nothing worth taking.", proposals=[]),
    )

    beat = _heartbeat(logs)
    assert beat["proposals"] == 0
    assert beat["approved"] == 0
    assert beat["executed"] == 0


def test_heartbeat_carries_the_figures_worth_glancing_at(monkeypatch, tmp_path):
    """Enough to answer "is it alive and is it sane" without opening the audit file."""
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Standing pat.", proposals=[]),
    )

    beat = _heartbeat(logs)
    for field in (
        "equity_usd",
        "open_positions",
        "open_risk_usd",
        "proposals",
        "approved",
        "executed",
        "stand_down_stage",
        "risk_understated",
        "cost_usd",
        "next_cycle_seconds",
    ):
        assert field in beat, f"heartbeat is missing {field}"

    assert beat["equity_usd"] > 0
    assert beat["cost_usd"] == 0.002717


def test_heartbeat_counts_proposals_separately_from_approvals(monkeypatch, tmp_path):
    """A rejected proposal must still be counted as proposed.

    Collapsing the two would hide the case that matters most — a model
    repeatedly proposing trades the gate keeps refusing.
    """
    from bot.models import Direction, OrderProposal

    # Rejected on size: 900 shares of SPY is far past every cap on a $100k account.
    oversized = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=900,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Deliberately oversized so the gate refuses it.",
    )

    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Leaning long.", proposals=[oversized]),
    )

    beat = _heartbeat(logs)
    assert beat["proposals"] == 1
    assert beat["approved"] == 0, "the gate should have refused this"
    assert beat["executed"] == 0


def test_nothing_is_executed_without_the_execute_flag(monkeypatch, tmp_path):
    """`executed` stays zero in a dry run even when the gate approves.

    This is the property the whole handover rests on, so it gets asserted from
    the loop rather than inferred from the flag's name.
    """
    from bot.models import Direction, OrderProposal

    modest = OrderProposal(
        symbol="SPY",
        direction=Direction.BUY,
        qty=3,
        limit_price=580.00,
        stop_loss_price=575.00,
        take_profit_price=590.00,
        rationale="Small enough to clear every cap.",
    )

    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Constructive.", proposals=[modest]),
    )

    assert any(e["event"] == "dry_run_no_orders_will_be_placed" for e in logs)
    assert _heartbeat(logs)["executed"] == 0
