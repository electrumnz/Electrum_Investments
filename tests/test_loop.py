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

import pytest
import structlog
from pydantic import ValidationError

import bot.main as main_mod
from bot.audit import AuditLog
from bot.claude_client import CallUsage, ClaudeDecision
from bot.config import Env, Rules, load_rules
from bot.dreaming import DreamStore
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
    # The loop opens the dream store to resolve symbol grants, and the shipped
    # `config/rules.yaml` turns grants on — so without this the cycle writes
    # `data/dreams.db` next to the real journal. The runtime-directory guard in
    # conftest catches that exactly once, on the run that first creates the
    # file, and is blind to it ever after.
    monkeypatch.setattr(main_mod, "DreamStore", lambda: DreamStore(tmp_path / "dreams.db"))

    env = Env(_env_file=None)  # type: ignore[call-arg]
    rules = load_rules()
    # These tests are about what the cycle decides, not about when it runs. Left
    # on, the market-closed skip would make them pass or fail according to the
    # clock on the machine running them.
    rules.loop.skip_model_call_when_all_markets_closed = False

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(env, rules, execute=False, force_mock=True) == 0
    return logs


def _heartbeat(logs: list[MutableMapping[str, Any]]) -> MutableMapping[str, Any]:
    beats = [e for e in logs if e["event"] == "cycle_complete"]
    assert len(beats) == 1, f"expected exactly one heartbeat, got {len(beats)}"
    return beats[0]


class _ExplodingClaude:
    """Fails the way the real client failed on the droplet.

    A `ValidationError` from the SDK's structured-output parsing is neither an
    `APIError` nor a timeout, and it is raised after a successful HTTP call, so
    no amount of network handling catches it.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def propose(self, market_context: str) -> tuple[ClaudeDecision, CallUsage]:
        raise self._error


def _run_one_cycle_with_client(
    monkeypatch, tmp_path, client: object, *, in_session: bool | None = None
) -> list[Any]:
    """Run one cycle with an arbitrary Claude stand-in.

    `in_session` pins whether any instrument class is open. Without it these
    tests would pass or fail depending on what time of day the suite ran, which
    is exactly the kind of test nobody trusts by the third flake.
    """

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "journal.db"))
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(main_mod, "ClaudeClient", lambda *a, **k: client)
    monkeypatch.setattr(main_mod, "DreamStore", lambda: DreamStore(tmp_path / "dreams.db"))

    env = Env(_env_file=None)  # type: ignore[call-arg]
    rules = load_rules()
    if in_session is None:
        rules.loop.skip_model_call_when_all_markets_closed = False
    else:
        monkeypatch.setattr(
            Rules, "any_class_in_session", lambda self, moment: in_session
        )

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(env, rules, execute=False, force_mock=True) == 0
    return logs


def test_a_closed_market_skips_the_model_call_but_still_reconciles(
    monkeypatch, tmp_path
):
    """The cost control. Nothing enabled is open, so no proposal could be approved.

    The stand-in explodes if called, so this fails loudly if the skip ever stops
    skipping rather than passing on a technicality.
    """
    logs = _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _ExplodingClaude(AssertionError("the model must not be called when shut")),
        in_session=False,
    )

    skips = [e for e in logs if e["event"] == "cycle_skipped_market_closed"]
    assert len(skips) == 1
    # Distinguishable from a cycle that ran and decided to do nothing, and from
    # one that never ran at all.
    assert not [e for e in logs if e["event"] == "cycle_complete"]
    assert not [e for e in logs if e["event"] == "model_call_failed"]
    # Still reports the state an operator glances at, so a shut market does not
    # mean a blank log.
    assert "open_risk_usd" in skips[0]
    assert "risk_understated" in skips[0]


def test_an_open_market_still_calls_the_model(monkeypatch, tmp_path):
    """The other half. A skip that fires while the market is open is a dead bot."""
    logs = _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _StubClaude(ClaudeDecision(market_assessment="Open and quiet.", proposals=[])),
        in_session=True,
    )

    assert [e for e in logs if e["event"] == "cycle_complete"]
    assert not [e for e in logs if e["event"] == "cycle_skipped_market_closed"]


@pytest.mark.parametrize(
    "error",
    [
        ValidationError.from_exception_data("ClaudeDecision", []),
        TimeoutError("read timed out"),
        Exception("APIError: overloaded"),
    ],
    ids=["validation_error", "timeout", "api_error"],
)
def test_a_failed_model_call_degrades_the_cycle_rather_than_ending_the_loop(
    monkeypatch, tmp_path, error
):
    """Observed live: one rationale came back over the cap and the loop died.

    A `ValidationError` propagating out of `propose` killed the process, and
    systemd restarted it straight into the same failure. That was survivable
    while the loop placed no orders. With `--execute` on it means real orders
    resting at the broker, the journal no longer reconciled and open positions
    no longer watched, with nothing on screen to say the bot has gone.
    """
    logs = _run_one_cycle_with_client(
        monkeypatch, tmp_path, _ExplodingClaude(error)
    )

    failures = [e for e in logs if e["event"] == "model_call_failed"]
    assert len(failures) == 1
    # Named, not swallowed. A cycle that produced no decision must not be
    # recorded as a cycle that decided to do nothing.
    assert not [e for e in logs if e["event"] == "cycle_complete"]


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


# --------------------------------------------------------- dream symbol grants


def test_the_heartbeat_states_which_symbols_a_dream_is_widening(monkeypatch, tmp_path):
    """A permission in force that is never stated is a permission nobody can
    audit.

    On the cycle line for the same reason `stops_breached` is: an empty list
    every cycle is a stated fact, where silence is also what an outage looks
    like.
    """
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Quiet, and nothing adopted.", proposals=[]),
    )

    beat = _heartbeat(logs)
    assert beat["granted_symbols"] == []


def test_an_adopted_dream_reaches_the_heartbeat(monkeypatch, tmp_path):
    """End to end through the wiring the loop actually uses: a dream adopted in
    the store shows up as a symbol the allowlist has been widened by."""
    from datetime import UTC, datetime

    from bot.dreaming import DREAMER, Dream, Vault

    store = DreamStore(tmp_path / "dreams.db")
    dream_id = store.save(
        Dream(title="t", seed="s", symbols=["TSLA"], asset_class_key="us_equity")
    )
    assert store.move(dream_id, Vault.VAULT, by=DREAMER)
    assert store.adopt(dream_id, at=datetime.now(UTC)).ok

    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ClaudeDecision(market_assessment="Quiet, with one dream adopted.", proposals=[]),
    )

    beat = _heartbeat(logs)
    assert beat["granted_symbols"] == ["TSLA"]


def test_a_broken_dream_store_costs_the_grants_and_not_the_cycle(monkeypatch, tmp_path):
    """Same rule as `fetch_market_ticks` catching broadly, for the same reason.

    An exception out of the store would end the decision loop — the journal
    stops being reconciled and open positions stop being watched, with real
    orders resting at the broker and nothing on screen to say the bot has gone.
    A store that will not open costs the permissions and nothing else.
    """

    def _explode() -> DreamStore:
        raise RuntimeError("unable to open database file")

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    # Set up by hand rather than through `_run_one_cycle`, which installs its
    # own tmp_path store and would overwrite the failing one.
    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "journal.db"))
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(main_mod, "DreamStore", _explode)
    monkeypatch.setattr(
        main_mod,
        "ClaudeClient",
        lambda *a, **k: _StubClaude(
            ClaudeDecision(market_assessment="The store is unavailable.", proposals=[])
        ),
    )

    rules = load_rules()
    rules.loop.skip_model_call_when_all_markets_closed = False

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(
            Env(_env_file=None),  # type: ignore[call-arg]
            rules,
            execute=False,
            force_mock=True,
        ) == 0

    beat = _heartbeat(logs)
    assert beat["granted_symbols"] == []
    assert [e for e in logs if e["event"] == "dream_store_unavailable"]


def test_grants_switched_off_never_open_the_store(monkeypatch, tmp_path):
    """`allow_symbol_grants: false` is the one-word revert, and it has to mean
    the store is not touched at all."""
    opened: list[str] = []

    def _record() -> DreamStore:
        opened.append("opened")
        return DreamStore(tmp_path / "dreams.db")

    monkeypatch.setattr(main_mod, "DreamStore", _record)

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: Journal(tmp_path / "journal.db"))
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(
        main_mod,
        "ClaudeClient",
        lambda *a, **k: _StubClaude(
            ClaudeDecision(market_assessment="Grants are off.", proposals=[])
        ),
    )

    rules = load_rules()
    rules.loop.skip_model_call_when_all_markets_closed = False
    rules.dreaming.allow_symbol_grants = False

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(
            Env(_env_file=None),  # type: ignore[call-arg]
            rules,
            execute=False,
            force_mock=True,
        ) == 0

    assert opened == []
    assert _heartbeat(logs)["granted_symbols"] == []
