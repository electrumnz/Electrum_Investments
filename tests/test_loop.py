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

import re
import sqlite3
import time
from collections.abc import MutableMapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

import bot.main as main_mod
from bot import jobs
from bot.audit import AuditLog
from bot.confer import CONFERENCE
from bot.config import Env, Rules, load_rules
from bot.dreaming import DreamStore, Hop, Vault
from bot.journal import Journal
from bot.model_client import CallUsage, ModelDecision
from bot.models import Decision, IndicatorSnapshot, MarketInputs


class _StubClaude:
    """Stands in for ModelClient. Returns whatever decision the test asked for.

    `model_id` and `price_is_known` are here because the real client has them
    and the loop reads both — one for the `loop_start` record, one to decide
    whether a cost can be stated at all. A double that is missing an attribute
    the original has pins a path production never takes, which is the trap
    `Broker.orders_degraded` was found by.
    """

    #: What a call costs, or `None` for a model with no prices on file.
    cost: float | None = 0.002717

    def __init__(self, decision: ModelDecision) -> None:
        self._decision = decision

    @property
    def model_id(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def price_is_known(self) -> bool:
        return self.cost is not None

    #: What the far end said answered. Defaults to agreement with `model_id`,
    #: because that is what an ordinary call looks like and a double that
    #: differs from the original in its resting state pins a path production
    #: never takes — the trap `Broker.orders_degraded` was found by. A test
    #: about a substitution overrides it.
    served: str = "claude-haiku-4-5-20251001"

    def propose(self, market_context: str) -> tuple[ModelDecision, CallUsage]:
        return self._decision, CallUsage(
            input_tokens=2072,
            output_tokens=129,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=self.cost,
            served_model_id=self.served,
            requested_model_id=self.model_id,
        )


class _UnpricedClaude(_StubClaude):
    """A model nobody has prices on file for. Everything else is identical."""

    cost = None


def _run_one_cycle(
    monkeypatch, tmp_path, decision: ModelDecision, client_cls: type = _StubClaude
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
    monkeypatch.setattr(main_mod, "ModelClient", lambda *a, **k: client_cls(decision))
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

    @property
    def model_id(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def price_is_known(self) -> bool:
        return True

    def propose(self, market_context: str) -> tuple[ModelDecision, CallUsage]:
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
    monkeypatch.setattr(main_mod, "ModelClient", lambda *a, **k: client)
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
        _StubClaude(ModelDecision(market_assessment="Open and quiet.", proposals=[])),
        in_session=True,
    )

    assert [e for e in logs if e["event"] == "cycle_complete"]
    assert not [e for e in logs if e["event"] == "cycle_skipped_market_closed"]


@pytest.mark.parametrize(
    "error",
    [
        ValidationError.from_exception_data("ModelDecision", []),
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
        ModelDecision(market_assessment="Thin tape, nothing worth taking.", proposals=[]),
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
        ModelDecision(market_assessment="Standing pat.", proposals=[]),
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
        ModelDecision(market_assessment="Leaning long.", proposals=[oversized]),
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
        ModelDecision(market_assessment="Constructive.", proposals=[modest]),
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
        ModelDecision(market_assessment="Quiet, and nothing adopted.", proposals=[]),
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
        ModelDecision(market_assessment="Quiet, with one dream adopted.", proposals=[]),
    )

    beat = _heartbeat(logs)
    assert beat["granted_symbols"] == ["TSLA"]
    assert beat["grant_state"] == "granted"
    assert beat["grants_degraded"] is False


def test_the_heartbeat_says_WHY_the_granted_list_is_empty(monkeypatch, tmp_path):
    """Five causes, one blank list, and the `calendar_degraded` lesson again.

    A switched-off feature, nothing adopted, a store that would not open, an
    unreadable row and a set over the cap all render `granted_symbols=[]`. Only
    two of those are ordinary, and a reader scanning the log to find out what
    the bot could see reaches for the wrong one every time.
    """
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quiet, and nothing adopted.", proposals=[]),
    )

    beat = _heartbeat(logs)
    assert beat["granted_symbols"] == []
    assert beat["grants_degraded"] is False
    assert beat["grant_state"] == "none_live"


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
        "ModelClient",
        lambda *a, **k: _StubClaude(
            ModelDecision(market_assessment="The store is unavailable.", proposals=[])
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
    # And the heartbeat says the list is empty because nothing could be read,
    # rather than leaving it to look like a quiet day with nothing adopted.
    assert beat["grants_degraded"] is True
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
        "ModelClient",
        lambda *a, **k: _StubClaude(
            ModelDecision(market_assessment="Grants are off.", proposals=[])
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


# ------------------------------------------------------- the vault commands
#
# `vault` is read-only, `vault-expire` marks and never deletes, and `confer` is
# deliberately unreachable from `cmd_loop`. All three are exercised without a
# network: `run_conference` is stubbed, and every store is a tmp_path.


def _dream_store(monkeypatch, tmp_path) -> DreamStore:
    """A dream store in tmp_path, wired where `main` resolves the name.

    Patched on `main` rather than on `bot.dreaming`, because the commands share
    the module-level name the decision loop resolves grants through — and the
    symptom of getting it wrong is a real `data/dreams.db` written beside the
    production journal, which the conftest guard catches exactly once.
    """
    store = DreamStore(tmp_path / "dreams.db")
    monkeypatch.setattr(main_mod, "DreamStore", lambda *a, **kw: store)
    return store


def _vaulted(store: DreamStore, **overrides) -> int:
    from bot.dreaming import DREAMER, Dream, Hop, Vault

    fields: dict[str, Any] = {
        "title": "Cicada broods and the marginal sesame supplier",
        "seed": "Two of the three largest producers sit inside overlapping broods",
        "chain": [Hop(claim="Broods emerge on fixed cycles", checked=True, source="USDA")],
        "symbols": ["SESM"],
        "asset_class_key": "us_equity",
    }
    fields.update(overrides)
    dream_id = store.save(Dream(**fields))
    assert store.move(dream_id, Vault.VAULT, by=DREAMER).ok
    return dream_id


def test_vault_prints_every_shelf_and_changes_nothing(monkeypatch, tmp_path, capsys):
    """Read-only, and an empty shelf says so out loud.

    A blank space under a heading is what an unreadable store would also look
    like, so "(empty)" is printed rather than inferred — the same rule as the
    zero on every `cycle_complete` line.
    """
    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _vaulted(store)
    before = store.get(dream_id)

    rules = load_rules()
    assert main_mod.cmd_vault(rules) == 0

    out = capsys.readouterr().out
    for shelf in ("workbench", "prophecy", "vault", "adopted", "archive"):
        assert shelf in out
    assert "(empty)" in out
    assert "Cicada broods" in out
    assert "Actually in force:         none" in out

    after = store.get(dream_id)
    assert after is not None and before is not None
    # Nothing moved, and the expiry clock was not touched.
    assert after.vault is before.vault
    assert after.vault_entered_at == before.vault_entered_at


def test_vault_separates_what_is_claimed_from_what_is_in_force(
    monkeypatch, tmp_path, capsys
):
    """Crypto is configured while disabled, so a dream naming it grants nothing.

    Printing only the store's answer would overstate the permission; printing
    only the resolved one would hide a dream asking for something the account
    has switched off.
    """
    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _vaulted(store, symbols=["DOGE/USD"], asset_class_key="crypto")
    assert store.adopt(dream_id).ok

    assert main_mod.cmd_vault(load_rules()) == 0

    out = capsys.readouterr().out
    assert "Claimed by live adoptions: DOGE/USD (crypto)" in out
    assert "Actually in force:         none" in out


def test_vault_names_an_adoption_whose_grant_is_already_dead(
    monkeypatch, tmp_path, capsys
):
    """The `dream-expired-holding` state, said out loud rather than left silent.

    The dream is still on the adopted shelf, its own 90-day TTL still has
    months to run, and the permission it implies died days ago. A readout that
    printed only the shelf count would let a reader take the grant to be in
    force, which is the confident-wrong-figure failure in a new place.
    """
    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _vaulted(store)
    assert store.adopt(
        dream_id, at=datetime.now(UTC) - timedelta(days=5), ttl_days=1
    ).ok

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_vault(load_rules()) == 0

    out = capsys.readouterr().out
    assert "ALREADY lapsed" in out
    assert "Any position" in out
    line = next(e for e in logs if e["event"] == "vault_listed")
    assert line["grants_already_lapsed"] == 1


def test_vault_expire_withdraws_a_lapsed_grant_and_deletes_nothing(
    monkeypatch, tmp_path, capsys
):
    """Expiry has teeth on an adopted dream and only on that.

    The grant goes, the dream comes back to the vault with a stated reason, and
    every hop, thought and message stays exactly where it was. Nothing is
    closed: a position opened under the lapsed grant is untouched, because
    expiry withdraws the right to OPEN and an unattended auto-close is an
    execution path nobody chose.
    """
    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _vaulted(store)
    # Adopted with a one-day grant, backdated so it has already lapsed.
    assert store.adopt(
        dream_id, at=datetime.now(UTC) - timedelta(days=3), ttl_days=1
    ).ok
    assert store.granted_symbols(datetime.now(UTC)) == {}  # dead by arithmetic

    rules = load_rules()
    assert main_mod.cmd_vault_expire(rules) == 0

    dream = store.get(dream_id)
    assert dream is not None
    assert dream.vault is Vault.VAULT
    assert dream.chain  # the reasoning survives, in full
    out = capsys.readouterr().out
    assert "grant withdrawn" in out
    assert "SESM" in out
    assert store.granted_symbols(datetime.now(UTC)) == {}
    assert [a.returned_at is not None for a in store.adoptions(dream_id)] == [True]


def test_vault_expire_marks_an_old_dream_once_per_stay(monkeypatch, tmp_path, capsys):
    """Idempotent, because this is the kind of command that ends up on a timer.

    The mark is written once per stay on a shelf. A second run the same day
    writes nothing and reports nothing new, so a daily timer does not fill the
    transcript a human reads with the same sentence a hundred times.
    """
    from bot.dreaming import Dream

    store = _dream_store(monkeypatch, tmp_path)
    stale = datetime.now(UTC) - timedelta(days=400)
    dream_id = store.save(
        Dream(title="an abandoned chain", seed="...", vault_entered_at=stale)
    )

    rules = load_rules()
    assert main_mod.cmd_vault_expire(rules) == 0
    first = store.messages(dream_id)
    assert [m.kind for m in first] == [main_mod.EXPIRY_MARK_KIND]
    # The machine narrating itself, never wearing the operator's name: an
    # operator note is one of the things that makes a dream worth conferring
    # again, and a mark that tripped the change gate would hand the two agents
    # a fresh exchange about a dream whose only news is that it got old.
    assert first[0].speaker == CONFERENCE

    assert main_mod.cmd_vault_expire(rules) == 0
    assert len(store.messages(dream_id)) == 1
    # Still there. Marks; never deletes.
    assert store.get(dream_id) is not None


# ------------------------------- the observation worklist, and answering one


def _observed(store: DreamStore, *, due_in_days: float = 20.0, **overrides) -> int:
    """A keep whose weakest link is pinned by an OPERATOR-settled observation.

    The shape TODO 0b exists for: nothing here is a number, because the weakest
    hop of a supply-chain chain never is.
    """
    from bot.dreaming import Dream, DreamCondition, DreamVerdict, Hop

    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "title": "Low water lifts the marginal barge operator",
        "seed": "Gauge readings at Memphis are near record lows",
        "chain": [
            Hop(claim="Low water forces draft restrictions", checked=True, source="USACE"),
            Hop(claim="Dry-cargo barging is material to KEX", checked=False),
        ],
        "verdict": DreamVerdict.KEEP,
        "weakest_hop": "is dry-cargo barging material to KEX",
        "weakest_hop_index": 2,
        "symbols": ["KEX"],
        "asset_class_key": "us_equity",
        "conditions": [
            DreamCondition(
                text="Dry cargo is a double-digit share of Kirby's revenue",
                subject="Kirby Corp's most recent 10-Q, segment note",
                observable="dry-cargo revenue as a share of total revenue",
                observe_by=now + timedelta(days=due_in_days),
                settles_hops=(2,),
            )
        ],
    }
    fields.update(overrides)
    return store.save(Dream(**fields))


def test_observations_lists_what_is_waiting_on_a_person(monkeypatch, tmp_path, capsys):
    """The command that exists because this half fails SILENTLY.

    A threshold is graded by code on every dream run whether or not anybody is
    watching. An observation is graded by somebody looking at the world, and
    somebody who is never asked never looks — so without this the prophecy
    shelf becomes a place dreams wait forever while every surface reports
    patience.
    """
    store = _dream_store(monkeypatch, tmp_path)
    _observed(store)

    assert main_mod.cmd_observations() == 0

    out = capsys.readouterr().out
    assert "Kirby Corp's most recent 10-Q" in out
    assert "1 awaiting, 0 overdue" in out
    # The handle is what `settle` takes, so it has to be on screen.
    assert re.search(r"\[[0-9a-f]{6}\]", out)


def test_an_empty_worklist_does_not_read_as_nothing_being_stuck(
    monkeypatch, tmp_path, capsys
):
    """`has_cycles` and `can_grade_anything`, arriving at a terminal.

    "Nothing is waiting on you" is true and is not "no dream is stuck": a dream
    can equally be held by a threshold the market has not reached, which nobody
    can settle by looking at anything. Letting the first sentence stand for the
    second is the confident partial answer this repository refuses.
    """
    _dream_store(monkeypatch, tmp_path)

    assert main_mod.cmd_observations() == 0

    out = capsys.readouterr().out
    assert "Nothing is waiting on you" in out
    assert "not that no dream is stuck" in out


def test_an_overdue_observation_is_reported_as_late_not_as_refuted(
    monkeypatch, tmp_path, capsys
):
    """OVERDUE is a fact about the LOOKING, never about the world.

    An unopened dashboard must not read as a refuted prophecy, so the row says
    the review date has passed and says nothing whatever about the claim.
    """
    store = _dream_store(monkeypatch, tmp_path)
    _observed(store, due_in_days=-3.0)

    assert main_mod.cmd_observations() == 0

    out = capsys.readouterr().out
    assert "OVERDUE" in out
    assert "1 awaiting, 1 overdue" in out
    # Still awaiting. An elapsed date does not settle anything either way.
    assert "ruled out" not in out.lower()


def test_settle_records_the_answer_and_carries_the_dream_up_a_shelf(
    monkeypatch, tmp_path, capsys
):
    """The link that was missing: a prophecy nobody could answer never moved.

    Before this command an observation-only dream reached PROPHECY and stopped
    there for ever, because the only writer of an answer had no caller. This is
    the whole ladder, driven the way an operator drives it.
    """
    from bot.dreaming import promotion_for

    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _observed(store)

    parked = store.get(dream_id)
    assert parked is not None
    before = promotion_for(parked)
    assert before.moves and before.to is Vault.PROPHECY

    handle = main_mod._observation_worklist(store, datetime.now(UTC))[0].handle
    assert (
        main_mod.cmd_settle(handle, met=True, note="Q2 10-Q: dry cargo 21% of revenue")
        == 0
    )

    promoted = store.get(dream_id)
    assert promoted is not None
    after = promotion_for(promoted)
    assert after.moves and after.to is Vault.VAULT

    out = capsys.readouterr().out
    assert "Recorded MET" in out
    # What happens next is stated, because promotion runs on a different
    # command and an operator left guessing why the shelf did not move would
    # reasonably conclude the answer was not recorded.
    assert "electrum-bot dream" in out


def test_settle_refuses_an_answer_with_nothing_behind_it(monkeypatch, tmp_path, capsys):
    """The note is the only record of WHAT was seen.

    An answer with nothing behind it is indistinguishable afterwards from a
    mis-click that granted a symbol, and this is a link in the route that
    widens what may be traded.
    """
    store = _dream_store(monkeypatch, tmp_path)
    _observed(store)
    handle = main_mod._observation_worklist(store, datetime.now(UTC))[0].handle

    assert main_mod.cmd_settle(handle, met=True, note="   ") == 1
    assert "needs_reason" in capsys.readouterr().out


def test_settle_has_no_default_answer(monkeypatch, tmp_path, capsys):
    """There is no safe guess between yes and no.

    A default of `--met` manufactures confirmations; a default of `--ruled-out`
    refutes claims nobody meant to refute. So neither is the default and the
    command says which flag to add.
    """
    _dream_store(monkeypatch, tmp_path)

    assert main_mod.cmd_settle("abc123", met=None, note="anything") == 2
    assert "--met or --ruled-out" in capsys.readouterr().out


def test_a_handle_that_was_already_answered_says_so_rather_than_going_missing(
    monkeypatch, tmp_path, capsys
):
    """Two reasons a handle misses, and they want opposite next actions.

    "You answered this on Tuesday" and "the claim you were shown is no longer
    on this dream" are different facts. Reporting both as *not found* would
    leave the operator guessing which one they were looking at, which is the
    missing-versus-answered rule arriving at a terminal.
    """
    store = _dream_store(monkeypatch, tmp_path)
    _observed(store)
    handle = main_mod._observation_worklist(store, datetime.now(UTC))[0].handle

    assert main_mod.cmd_settle(handle, met=True, note="21% of revenue") == 0
    capsys.readouterr()

    assert main_mod.cmd_settle(handle, met=False, note="changed my mind") == 1
    out = capsys.readouterr().out
    assert "Already answered: met" in out
    assert "Neither answer is reversible" in out

    # And a handle for a claim that genuinely is not there reads differently.
    assert main_mod.cmd_settle("deadbe", met=True, note="x") == 1
    assert "Nothing on any shelf carries that claim" in capsys.readouterr().out


def test_the_handle_changes_when_the_claim_does(monkeypatch, tmp_path):
    """Derived from the claim, so a restated one cannot be answered by mistake.

    This looks like a drawback and is the guarantee. A dreamer that rewords the
    subject between the operator reading the worklist and answering it has
    written a NEW claim; an answer aimed at the old handle must land nowhere
    rather than land on something nobody was shown.
    """
    from bot.dreaming import observation_handle

    store = _dream_store(monkeypatch, tmp_path)
    dream_id = _observed(store)
    saved = store.get(dream_id)
    assert saved is not None
    original = saved.conditions[0]

    reworded = replace(original, text="the same claim, said differently")
    moved = replace(original, subject="a different filing entirely")

    # The prose is the claim's description, not the claim.
    assert observation_handle(dream_id, reworded) == observation_handle(
        dream_id, original
    )
    assert observation_handle(dream_id, moved) != observation_handle(dream_id, original)
    # And the same claim on a different dream is a different question.
    assert observation_handle(dream_id + 1, original) != observation_handle(
        dream_id, original
    )


def test_confer_refuses_without_an_api_key(monkeypatch, tmp_path):
    """Fail closed and say why, rather than a stack trace out of the SDK."""
    _dream_store(monkeypatch, tmp_path)
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = ""

    assert main_mod.cmd_confer(env, load_rules()) == 1


def test_confer_reports_the_run_and_exits_zero_on_a_quiet_one(
    monkeypatch, tmp_path
):
    """A quiet run is a success.

    An empty vault, or two dreams skipped because nothing has changed since the
    last exchange, is the arrangement working exactly as designed. Reporting
    that as a failed unit every morning is how an operator learns to ignore
    `systemctl --failed`.
    """
    import bot.confer as confer_mod

    _dream_store(monkeypatch, tmp_path)
    report = confer_mod.ConferenceReport(
        exchanges=(
            confer_mod.ExchangeResult(
                dream_id=1, outcome=confer_mod.ConferOutcome.NOTHING_NEW
            ),
        ),
        considered=1,
    )
    monkeypatch.setattr(confer_mod, "run_conference", lambda *a, **kw: report)

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test-key"

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_confer(env, load_rules()) == 0

    line = next(e for e in logs if e["event"] == "confer_complete")
    assert line["conferred"] == 0
    assert line["skipped"] == 1
    assert line["adopted"] == 0
    assert line["calls"] == 0
    assert line["cost_usd"] == 0.0


def test_confer_exits_non_zero_when_every_call_failed(monkeypatch, tmp_path):
    """A report full of CALL_FAILED is the one hard failure worth a timer's
    attention: the model could not be reached at all, so nothing was decided
    and nothing will be until somebody looks."""
    import bot.confer as confer_mod

    _dream_store(monkeypatch, tmp_path)
    report = confer_mod.ConferenceReport(
        exchanges=(
            confer_mod.ExchangeResult(
                dream_id=1, outcome=confer_mod.ConferOutcome.CALL_FAILED
            ),
            confer_mod.ExchangeResult(
                dream_id=2, outcome=confer_mod.ConferOutcome.CALL_FAILED
            ),
        ),
        considered=2,
    )
    monkeypatch.setattr(confer_mod, "run_conference", lambda *a, **kw: report)

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = "test-key"

    assert main_mod.cmd_confer(env, load_rules()) == 1


def test_the_loop_never_confers(monkeypatch, tmp_path):
    """The separation is a command boundary, and this is what holds it there.

    Ninety-six unattended negotiations a day, on the same process that places
    orders, is the Alpha Arena failure shape with two models instead of one. A
    later refactor that reached for a conference from inside the cycle would
    pass every other test in this file.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(main_mod.cmd_loop)))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not used & {"run_conference", "Conference", "cmd_confer", "confer"}


# --------------------------- the feeds run over the WIDENED symbol set
#
# The gate honoured a grant and nothing else did. The prompt listed
# `rules.allowed_symbols` and the tick, indicator, intraday and news fetches all
# ran over the same list, so a granted symbol had no quote and no history and a
# proposal in one would have been dropped before it reached the gate that would
# have allowed it. These tests are about the ORDERING as much as the set: the
# grant used to be resolved after the feeds had already run.


def _adopt(store: DreamStore, symbol: str = "TSLA") -> int:
    from bot.dreaming import DREAMER, Dream

    dream_id = store.save(
        Dream(
            title="t",
            seed="s",
            symbols=[symbol],
            asset_class_key="us_equity",
            chain=[Hop("a claim nobody checked", False, "")],
            weakest_hop="the first hop",
        )
    )
    assert store.move(dream_id, Vault.VAULT, by=DREAMER)
    assert store.adopt(dream_id, at=datetime.now(UTC)).ok
    return dream_id


def test_a_granted_symbol_is_fetched_a_tick_and_indicators_like_any_other(
    monkeypatch, tmp_path
):
    """**Verified to fail when the feeds are narrowed back to `allowed_symbols`.**

    Without this the permission is unusable: no tick means the loop drops the
    proposal with `no_tick_for_proposal` before `RiskGate.evaluate` is ever
    called.
    """
    from bot.context import fetch_market_ticks as real_ticks

    seen: list[list[str]] = []

    def _record(broker, symbols):
        seen.append(list(symbols))
        return real_ticks(broker, symbols)

    monkeypatch.setattr(main_mod, "fetch_market_ticks", _record)
    _adopt(DreamStore(tmp_path / "dreams.db"))

    _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="One dream adopted.", proposals=[]),
    )

    assert seen, "the tick fetch never ran"
    assert "TSLA" in seen[0]
    assert "SPY" in seen[0], "the allowlist must still be fetched alongside it"


def test_the_earnings_calendar_is_REBUILT_for_a_granted_symbol_not_mutated(
    monkeypatch, tmp_path
):
    """`FinnhubCalendar` is constructed once with `rules.allowed_symbols`, so the
    news blackout could never fire for a granted symbol — the gate's logic was
    fine and its input was narrowed.

    It must be a rebuild rather than an assignment to `.symbols`: the feed caches
    windows it has already filtered against its symbol list, so mutating the
    attribute leaves that cache in place, which looks fixed and behaves
    inconsistently. That is worse than the open gap.
    """
    built: list[list[str]] = []

    def _build(env, rules, *, extra_symbols=()):
        built.append(sorted(set(rules.allowed_symbols) | set(extra_symbols)))
        return _NoWindows()

    monkeypatch.setattr(main_mod, "build_calendar_feed", _build)
    _adopt(DreamStore(tmp_path / "dreams.db"))

    _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="One dream adopted.", proposals=[]),
    )

    # Built twice: once at loop start on the allowlist, once when the grant
    # widened the set. The second build is the one that closes the gap.
    assert len(built) == 2, built
    assert "TSLA" not in built[0]
    assert "TSLA" in built[1]


class _NoWindows:
    def upcoming_windows(self, *, lookahead_minutes: int):
        del lookahead_minutes
        return []


def test_the_granted_symbol_and_its_chain_reach_the_market_context(
    monkeypatch, tmp_path
):
    """A permission the model is never told about is a permission nothing uses.

    And the chain must arrive labelled — badge and weakest hop adjacent — because
    an unqualified speculative chain in a prompt reads as established fact.
    """
    from bot.context import build_market_context as real_context

    contexts: list[str] = []

    def _record(**kwargs):
        blob = real_context(**kwargs)
        contexts.append(blob)
        return blob

    monkeypatch.setattr(main_mod, "build_market_context", _record)
    _adopt(DreamStore(tmp_path / "dreams.db"))

    _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="One dream adopted.", proposals=[]),
    )

    assert contexts
    blob = contexts[0]
    assert "TSLA (us_equity)" in blob
    assert "permission ends" in blob
    assert "UNVERIFIED" in blob
    assert "WEAKEST HOP: the first hop" in blob
    assert "does NOT propose a position" in blob


# ------------------------------------------- readings a prophecy is graded on


def test_recent_readings_come_back_oldest_first(tmp_path):
    """`AuditLog.read` is newest-first and `grade_conditions` needs ascending.

    Reversed, every condition would be stamped with the most recent moment it
    held rather than the moment it became true — which is the whole value of
    pre-registering a claim.
    """
    audit = AuditLog(tmp_path / "audit")
    for hour, close in ((9, 90.0), (10, 110.0)):
        audit.record(
            Decision(
                timestamp=datetime(2026, 6, 1, hour, tzinfo=UTC),
                inputs=MarketInputs(readings={"AA": IndicatorSnapshot(close=close)}),
            )
        )

    readings = main_mod.recent_readings(audit.read())

    assert [c.at.hour for c in readings] == [9, 10]
    assert readings[0].readings["AA"].close == 90.0


def test_a_cycle_that_recorded_no_numeric_readings_is_skipped(tmp_path):
    """`MarketInputs.readings` could not be backfilled: a cycle written before it
    shipped carries prose about figures and no figures. Counting those as
    cycles-with-no-reading would make `can_grade_anything` claim evidence that is
    not there."""
    audit = AuditLog(tmp_path / "audit")
    audit.record(
        Decision(timestamp=datetime(2026, 6, 1, 9, tzinfo=UTC), inputs=MarketInputs())
    )
    audit.record(Decision(timestamp=datetime(2026, 6, 1, 10, tzinfo=UTC), inputs=None))

    assert main_mod.recent_readings(audit.read()) == []


# --------------------------- acting on a position, and saying what was not done


def _cycle_with_broker(
    monkeypatch,
    tmp_path,
    decision: ModelDecision,
    broker: Any,
    *,
    execute: bool = False,
    position_actions: bool = False,
    journal: Journal | None = None,
) -> list[MutableMapping[str, Any]]:
    """One cycle against a broker the test built, with both switches settable.

    `_run_one_cycle` builds its own `MockBroker` inside `build_broker`, so a
    test that needs a POSITION — which is the only thing a position plan can
    act on — has no way to reach it. This swaps the factory instead, the same
    way `tests/test_web.py` primes the poller.
    """

    def _stop(_seconds: float) -> None:
        raise KeyboardInterrupt

    store = journal or Journal(tmp_path / "journal.db")
    monkeypatch.setattr(time, "sleep", _stop)
    monkeypatch.setattr(main_mod, "Journal", lambda: store)
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))
    monkeypatch.setattr(main_mod, "ModelClient", lambda *a, **k: _StubClaude(decision))
    monkeypatch.setattr(main_mod, "DreamStore", lambda: DreamStore(tmp_path / "dreams.db"))
    monkeypatch.setattr(main_mod, "build_broker", lambda env, force_mock=False: broker)

    env = Env(_env_file=None)  # type: ignore[call-arg]
    rules = load_rules()
    rules.loop.skip_model_call_when_all_markets_closed = False
    rules.position_actions.enabled = position_actions

    with structlog.testing.capture_logs() as logs:
        assert main_mod.cmd_loop(env, rules, execute=execute, force_mock=True) == 0
    return logs


def _short_spy_at_the_broker(stop_leg: float | None = 805.0):
    """A held short with a stop leg resting at a level the journal will disagree
    with. The live position's own shape."""
    from bot.broker import MockBroker
    from bot.models import Direction, OrderStatus, Position, WorkingOrder

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=789.98, ask=790.02)
    broker._positions["SPY"] = Position(
        symbol="SPY",
        direction=Direction.SELL,
        qty=21,
        entry_price=773.32,
        opened_at=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
        current_price=790.0,
    )
    if stop_leg is not None:
        broker.set_open_orders(
            [
                WorkingOrder(
                    order_id="stop-1",
                    symbol="SPY",
                    direction=Direction.BUY,
                    qty=21,
                    order_type="stop",
                    stop_price=stop_leg,
                    status=OrderStatus.NEW,
                    submitted_at=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
                )
            ]
        )
    return broker


def _journal_with_the_short(tmp_path) -> Journal:
    from bot.models import Direction, Trade

    journal = Journal(tmp_path / "journal.db")
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=21,
            entry_time=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
            entry_price=773.32,
            planned_stop=820.0,
            rationale="Operator test short, journalled by hand.",
        )
    )
    return journal


def _tighten(new_stop: float | None = 810.0):
    from bot.models import PositionAction, PositionPlan

    return ModelDecision(
        market_assessment="Holding the short, pulling the stop in.",
        proposals=[],
        position_plans=[
            PositionPlan(
                symbol="SPY",
                action=PositionAction.TIGHTEN_STOP,
                reasoning="Price has come 17 points my way; taking the risk down.",
                new_stop_price=new_stop,
            )
        ],
    )


def test_the_heartbeat_states_the_unexplained_move_count(monkeypatch, tmp_path):
    """A stop resting at 805 against a journal that says 820, with nothing on
    file to explain the difference.

    On the cycle line rather than only in a warning, and for the same reason
    `stops_breached` is: a stated zero each cycle is a fact, where an absent
    field is what an outage looks like.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        _short_spy_at_the_broker(),
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    assert beat["unexplained_moves"] == 1
    assert beat["unexplained_check_ran"] is True
    assert beat["stops_unreadable"] == []
    assert beat["positions_without_a_resting_stop"] == []


def test_the_heartbeat_states_a_zero_rather_than_leaving_the_field_out(
    monkeypatch, tmp_path
):
    """The stop at the broker matches the journal, so there is nothing to
    report — and the field still has to be there saying so."""
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        _short_spy_at_the_broker(stop_leg=820.0),
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    assert beat["unexplained_moves"] == 0
    assert beat["unexplained_check_ran"] is True


def test_a_trailing_leg_is_named_on_the_cycle_line_and_not_counted_as_a_move(
    monkeypatch, tmp_path
):
    """A trail moving is the exit working, so it must not spend the tag that
    means "somebody moved this and said nothing" — and it must not be silent
    either, or "nothing differed" and "this one is not compared" look alike."""
    from bot.models import Direction, OrderStatus, WorkingOrder

    broker = _short_spy_at_the_broker(stop_leg=None)
    broker.set_open_orders(
        [
            WorkingOrder(
                order_id="trail-1",
                symbol="SPY",
                direction=Direction.BUY,
                qty=21,
                order_type="trailing_stop",
                stop_price=809.0,
                trail_percent=1.5,
                high_water_mark=797.0,
                status=OrderStatus.NEW,
                submitted_at=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
            )
        ]
    )
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        broker,
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    assert beat["unexplained_moves"] == 0
    assert beat["trailing_stops"] == ["SPY"]
    assert beat["unreadable_trails"] == []
    # A trailing leg is still a resting stop, so the position is not stopless.
    assert beat["positions_without_a_resting_stop"] == []


def test_the_cycle_line_carries_what_reconcile_found_about_the_entry(
    monkeypatch, tmp_path
):
    """`record_fill` writes the PROPOSAL's quantity and limit price, and
    `reconcile` is what squares that against the fill. Every one of those
    findings was reaching the `ReconcileResult` and no log line at all.

    Stated zeros, for the reason `stops_breached` is: an absent field is what
    an outage looks like, and these are the fields that say whether the open
    risk figure describes the account or describes what was asked for.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        _short_spy_at_the_broker(stop_leg=820.0),
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    for field in (
        "entries_corrected",
        "entries_resting",
        "entries_mid_fill",
        # The three that say the entry could NOT be squared, and they are three
        # different findings. `closes_deferred` names which symbol was held
        # back and never why, so without these a routine out-of-hours entry and
        # an order the broker says was cancelled read identically.
        "entries_unchecked",
        "entries_unresolved",
        "entries_never_opened",
        "closes_deferred",
        "plan_abandoned",
    ):
        assert field in beat, f"{field} is not on the cycle line"
    assert beat["entries_resting"] == []
    assert beat["entries_unresolved"] == []
    assert beat["entries_never_opened"] == []
    assert beat["plan_abandoned"] == 0


def test_a_shut_market_still_states_what_reconcile_found_about_the_entry(
    monkeypatch, tmp_path
):
    """The cycle these matter most on.

    Reconcile runs above the skip, and an order placed out of hours RESTS until
    the next regular open — so `entries_resting` and `closes_deferred` are at
    their most populated precisely while the market is closed. Carrying them
    only on `cycle_complete` would leave the field absent for the whole stretch
    it describes.
    """
    logs = _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _ExplodingClaude(AssertionError("the model must not be called when shut")),
        in_session=False,
    )

    skip = next(e for e in logs if e["event"] == "cycle_skipped_market_closed")
    for field in (
        "entries_corrected",
        "entries_resting",
        "entries_mid_fill",
        "entries_unchecked",
        "entries_unresolved",
        "entries_never_opened",
        "closes_deferred",
        "plan_abandoned",
    ):
        assert field in skip, f"{field} is absent while the market is shut"


def test_a_cancelled_entry_reaches_the_cycle_line_as_a_settled_fact(
    monkeypatch, tmp_path
):
    """The reason `closes_deferred` alone was not enough.

    An out-of-hours entry resting until the next open and an entry the broker
    cancelled hours ago both defer their close, so on `closes_deferred` they
    are the same symbol in the same list. Only one of them is holding planned
    risk the account will never lose, and only one needs somebody. The reason
    is on the line now, so a reader can tell them apart without opening the
    audit log.
    """
    from bot.broker import MockBroker
    from bot.models import Direction, FillState, Trade

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=789.98, ask=790.02)
    broker.set_open_orders([])                       # nothing resting
    broker.set_order("entry-1", broker_status="canceled")

    journal = Journal(tmp_path / "journal.db")
    journal.record_entry(
        Trade(
            symbol="SPY",
            direction=Direction.SELL,
            qty=21,
            entry_time=datetime(2026, 5, 4, 15, 0, tzinfo=UTC),
            entry_price=772.84,
            planned_stop=820.0,
            submitted_qty=21,
            submitted_price=772.84,
            fill_state=FillState.UNCONFIRMED,
            entry_order_id="entry-1",
            rationale="Journalled from the proposal, then cancelled at Alpaca.",
        )
    )

    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        broker,
        journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["entries_never_opened"] == ["SPY"]
    assert beat["entries_unresolved"] == []
    assert beat["closes_deferred"] == ["SPY"]
    # And it is still open in the journal, still counting its planned risk.
    assert [t.symbol for t in journal.open_trades()] == ["SPY"]
    assert journal.closed_trades() == []


def test_a_position_with_no_resting_stop_is_named_rather_than_counted_clean(
    monkeypatch, tmp_path
):
    """Not a move and not nothing: an equity position with no stop order behind
    it means rule 3 is being met by `stop_watch` and by nothing at the broker."""
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Holding.", proposals=[]),
        _short_spy_at_the_broker(stop_leg=None),
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    assert beat["unexplained_moves"] == 0
    assert beat["positions_without_a_resting_stop"] == ["SPY"]


def test_a_tighten_is_executed_when_both_switches_are_on(monkeypatch, tmp_path):
    """The wiring that was missing: nothing called `execute_position_plan` at
    all, so a plan naming a level did nothing whatever.

    Both halves are asserted — the broker leg is replaced AND the journal
    records the move with the model's reasoning as the reason, because a
    journal saying 810 with 805 still resting would be worse than no record.
    """
    journal = _journal_with_the_short(tmp_path)
    broker = _short_spy_at_the_broker(stop_leg=820.0)

    logs = _cycle_with_broker(
        monkeypatch, tmp_path, _tighten(810.0), broker,
        execute=True, position_actions=True, journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == ["SPY:tighten_stop"]
    assert beat["position_actions_not_taken"] == []

    trade = journal.open_trade_for("SPY")
    assert trade is not None
    assert trade.current_stop == 810.0
    assert trade.planned_stop == 820.0, "the sizing figure must not be overwritten"
    assert trade.current_risk_usd < trade.planned_risk_usd

    assert [o.stop_price for o in broker.get_open_orders()] == [810.0]
    action = journal.position_actions()[0]
    assert action.after_stop == 810.0
    assert action.reason.startswith("Price has come 17 points")
    # And the risk the cycle reports is re-read after the move, or the line
    # would state the figure the account carried before the loop acted.
    assert beat["open_risk_usd"] == pytest.approx(trade.current_risk_usd, abs=0.01)


def test_a_widening_plan_is_refused_and_the_position_is_untouched(
    monkeypatch, tmp_path
):
    """A short's stop tightens DOWNWARD, so 830 is a widen — the one position
    move that increases the loss at unchanged size, with no gate anywhere in
    this system that would catch it. Refused in code, and the loop says so."""
    journal = _journal_with_the_short(tmp_path)
    broker = _short_spy_at_the_broker(stop_leg=820.0)

    logs = _cycle_with_broker(
        monkeypatch, tmp_path, _tighten(830.0), broker,
        execute=True, position_actions=True, journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == []
    assert beat["position_actions_not_taken"] == ["SPY:tighten_stop"]

    trade = journal.open_trade_for("SPY")
    assert trade is not None and trade.current_stop is None
    assert [o.stop_price for o in broker.get_open_orders()] == [820.0]

    refusal = next(e for e in logs if e["event"] == "position_plan_refused")
    assert any("REFUSED" in r for r in refusal["reasons"])


def test_the_switch_being_off_is_stated_rather_than_silent(monkeypatch, tmp_path):
    """`position_actions.enabled` ships false, so the ordinary reading is that
    a plan is recorded and not acted on.

    It has to be SAID: a model proposing a tighten every cycle against a loop
    that silently ignores every one looks exactly like a loop whose plans are
    empty. The refusal comes back from `execute_position_plan` rather than
    being pre-empted here, which is what keeps that fail-closed check exercised
    on a real cycle rather than only in its own unit test.
    """
    journal = _journal_with_the_short(tmp_path)
    broker = _short_spy_at_the_broker(stop_leg=820.0)

    logs = _cycle_with_broker(
        monkeypatch, tmp_path, _tighten(810.0), broker,
        execute=True, position_actions=False, journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == []
    assert beat["position_actions_not_taken"] == ["SPY:tighten_stop"]

    state = next(e for e in logs if e["event"] == "position_actions_state")
    assert state["enabled"] is False
    assert "records its position plans and acts on none of them" in state["detail"]

    refusal = next(e for e in logs if e["event"] == "position_plan_refused")
    assert any("position_actions.enabled is false" in r for r in refusal["reasons"])

    trade = journal.open_trade_for("SPY")
    assert trade is not None and trade.current_stop is None
    assert [o.stop_price for o in broker.get_open_orders()] == [820.0]


def test_a_dry_run_reaches_the_broker_for_nothing_at_all(monkeypatch, tmp_path):
    """`electrum-bot loop` without `--execute` is documented as placing nothing,
    and a stop replace is a broker call like any other.

    So the plan is recorded and named as not taken, even with the operator's
    own switch on — the two switches are different questions and either being
    off is enough.
    """
    journal = _journal_with_the_short(tmp_path)
    broker = _short_spy_at_the_broker(stop_leg=820.0)

    logs = _cycle_with_broker(
        monkeypatch, tmp_path, _tighten(810.0), broker,
        execute=False, position_actions=True, journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == []
    assert beat["position_actions_not_taken"] == ["SPY:tighten_stop"]

    skipped = next(e for e in logs if e["event"] == "position_plan_not_executed")
    assert "--execute is off" in skipped["reason"]

    trade = journal.open_trade_for("SPY")
    assert trade is not None and trade.current_stop is None
    assert [o.stop_price for o in broker.get_open_orders()] == [820.0]


def test_a_hold_is_not_recorded_as_an_action(monkeypatch, tmp_path):
    """A hold is not a move. Writing a row for one every fifteen minutes for
    every open position would bury the moves that matter in the ones that are
    not moves at all — and it is already in the audit log with the cycle."""
    from bot.models import PositionAction, PositionPlan

    journal = _journal_with_the_short(tmp_path)
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Thesis intact.",
            proposals=[],
            position_plans=[
                PositionPlan(
                    symbol="SPY",
                    action=PositionAction.HOLD,
                    reasoning="Still short against the 820 level; nothing has changed.",
                )
            ],
        ),
        _short_spy_at_the_broker(stop_leg=820.0),
        execute=True,
        position_actions=True,
        journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == []
    assert beat["position_actions_not_taken"] == []
    assert journal.position_actions() == []


def test_a_failed_position_move_degrades_the_cycle_rather_than_ending_the_loop(
    monkeypatch, tmp_path
):
    """Every broker method here reports a refusal rather than raising, so the
    exception that reaches the loop is the unanticipated one.

    Letting it out would end the decision loop with real orders resting at the
    broker and nothing on screen to say the bot has gone — the same failure as
    the unwrapped model call, arriving on the position path. The position is
    left exactly as it was, which is the safe direction.
    """
    journal = _journal_with_the_short(tmp_path)
    broker = _short_spy_at_the_broker(stop_leg=820.0)

    def _explode(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_mod, "execute_position_plan", _explode)

    logs = _cycle_with_broker(
        monkeypatch, tmp_path, _tighten(810.0), broker,
        execute=True, position_actions=True, journal=journal,
    )

    beat = _heartbeat(logs)
    assert beat["position_actions_taken"] == []
    assert beat["position_actions_not_taken"] == ["SPY:tighten_stop"]

    failure = next(e for e in logs if e["event"] == "position_plan_failed")
    assert "database is locked" in failure["error"]

    trade = journal.open_trade_for("SPY")
    assert trade is not None and trade.current_stop is None
    assert [o.stop_price for o in broker.get_open_orders()] == [820.0]


# ---------------------------------------------------------------------------
# The loop's passes, recorded as jobs
#
# "Did the loop run at 14:15, and what happened?" used to need a grep through
# `journalctl`, and grep could only ever find the passes that FINISHED: a cycle
# skipped with the market shut and a cycle whose model call failed never reached
# the audit log at all.
#
# Every test below exists to hold three outcomes apart — ran-and-did-nothing,
# skipped, failed — plus the fourth state that is none of them: no record, which
# must never be readable as a quiet cycle. Same rule as `has_cycles` in
# `news_history`, `can_grade_anything` in `triggers` and `NO_DECISION` in
# `confer`.


def _jobs_after(tmp_path, **kwargs) -> jobs.JobHistory:
    """The job history the cycle just written left behind."""
    return jobs.read(AuditLog(tmp_path / "audit"), **kwargs)


def test_a_quiet_pass_is_recorded_as_a_job_that_ran_and_proposed_nothing(
    monkeypatch, tmp_path
):
    """The common case, and the one that has to be legible as a decision.

    Standing pat is a valid output. The record says the loop looked and chose
    nothing, which is a different fact from the loop not having looked.
    """
    _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Thin tape, nothing worth taking.", proposals=[]),
    )

    history = _jobs_after(tmp_path)
    assert [j.outcome for j in history.jobs] == [jobs.Outcome.RAN]
    job = history.jobs[0]
    assert job.proposals == 0
    assert job.approved == 0
    assert job.executed == 0
    # A real reading, so `quiet` is entitled to be true here and nowhere else.
    assert job.quiet is True
    assert history.ran == 1 and history.quiet == 1
    assert history.skipped == 0 and history.failed == 0


def test_a_skipped_pass_records_NO_COUNT_rather_than_a_zero(monkeypatch, tmp_path):
    """The distinction the whole record exists for.

    A skipped cycle did not propose nothing — it did not look. A zero here would
    make a shut market indistinguishable from a quiet one, and `record_skipped`
    has no parameter that could carry one.
    """
    _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _ExplodingClaude(AssertionError("the model must not be called when shut")),
        in_session=False,
    )

    history = _jobs_after(tmp_path)
    assert [j.outcome for j in history.jobs] == [jobs.Outcome.SKIPPED]
    job = history.jobs[0]
    assert job.proposals is None
    assert job.approved is None
    assert job.executed is None
    # Not merely absent from the counts: it must not be countable as quiet.
    assert job.quiet is False
    assert history.quiet == 0
    assert history.skipped == 1 and history.ran == 0
    # The account state is still on the record, so a skip reads as "the market
    # was shut" rather than as a blank pass.
    assert job.standing.equity_usd is not None
    assert job.standing.open_risk_usd is not None


def test_a_failed_pass_is_recorded_as_failed_and_never_as_one_that_held(
    monkeypatch, tmp_path
):
    """A cycle that could not get a decision must not be recorded as one that
    decided to do nothing. Observed live: a rationale came back over the cap,
    the SDK raised, and the loop died into a systemd restart loop."""
    _run_one_cycle_with_client(
        monkeypatch, tmp_path, _ExplodingClaude(TimeoutError("read timed out"))
    )

    history = _jobs_after(tmp_path)
    assert [j.outcome for j in history.jobs] == [jobs.Outcome.FAILED]
    job = history.jobs[0]
    assert job.proposals is None
    assert job.quiet is False
    assert "read timed out" in job.detail
    assert history.failed == 1 and history.ran == 0 and history.quiet == 0
    # The older named event stays: it carries the error under its own kind and
    # other readers already know about it.
    view = AuditLog(tmp_path / "audit").read()
    assert any(e.kind == "model_call_failed" for e in view.events)


def test_an_unpriced_cycle_reports_the_cost_as_unknown_rather_than_as_free(
    monkeypatch, tmp_path
):
    """**The missing-versus-zero rule, with money attached.**

    `CallUsage.estimated_cost_usd` is `float | None`, and a model with no entry
    in `config.MODEL_SPECS` gives `None`. Rounding that to 0.0 for the log line
    would make the one cycle nobody can price look like the cheapest cycle of
    the day, and an operator watching the running cost would read an unpriced
    model as a free one.

    The audit `Decision` is the awkward half and it is checked here too. Its
    `estimated_cost_usd` is a plain float on an append-only record that is never
    migrated, so it cannot hold "unknown" — the zero it stores is legible only
    because the token counts sit beside it and a call that spent tokens cannot
    have cost exactly nothing. `model_cost_unknown` is what turns that inference
    into a record, and it names the model so nobody has to deduce the cause.
    """
    logs = _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _UnpricedClaude(ModelDecision(market_assessment="Holding.", proposals=[])),
        in_session=True,
    )

    beat = _heartbeat(logs)
    assert beat["cost_usd"] is None, (
        "an unpriced cycle put a number on the heartbeat. A 0.0 there reads as "
        "free, which is the one thing it is not."
    )

    view = AuditLog(tmp_path / "audit").read()
    unpriced = [e for e in view.events if e.kind == "model_cost_unknown"]
    assert len(unpriced) == 1, "the unknown price left no durable record"
    assert unpriced[0].payload["model"] == "claude-haiku-4-5-20251001"
    assert unpriced[0].payload["input_tokens"] == 2072

    # And the record the Decisions page reads: tokens spent, cost stored as the
    # only value the field can hold. The renderer turns that pair into "cost
    # unknown"; see tests/test_web.py.
    assert view.decisions
    recorded = view.decisions[0].decision
    assert recorded.claude_input_tokens == 2072
    assert recorded.estimated_cost_usd == 0.0

    # The run's own model is on the record rather than only its tier, because a
    # named model is not one of three tiers.
    start = [e for e in view.events if e.kind == "loop_start"]
    assert start and start[0].payload["model"] == "claude-haiku-4-5-20251001"
    assert start[0].payload["cost_visible"] is False


def test_a_priced_cycle_still_reports_the_figure_and_writes_no_extra_event(
    monkeypatch, tmp_path
):
    """The other half, and the one that must not regress.

    Every shipped deployment runs a Claude tier with prices on file, so the
    normal path has to be untouched: a real figure on the heartbeat, and NO
    `model_cost_unknown` event. An event written every cycle on a healthy loop
    would be noise in the one record a person reads.
    """
    logs = _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _StubClaude(ModelDecision(market_assessment="Holding.", proposals=[])),
        in_session=True,
    )

    assert _heartbeat(logs)["cost_usd"] == pytest.approx(0.002717)

    view = AuditLog(tmp_path / "audit").read()
    assert not [e for e in view.events if e.kind == "model_cost_unknown"]
    assert view.decisions[0].decision.estimated_cost_usd == pytest.approx(0.002717)
    start = [e for e in view.events if e.kind == "loop_start"]
    assert start and start[0].payload["cost_visible"] is True


def test_the_bars_are_not_fetched_on_a_pass_that_will_not_ask_the_model(
    monkeypatch, tmp_path
):
    """The cost control that makes a wider `allowed_symbols` affordable.

    Each of the three feeds is one network call PER SYMBOL PER CYCLE, so the
    loop's data bill is linear in the allowlist. The quotes have to be paid
    whatever the market is doing — `stop_watch` and the expiry alerts read them,
    and out of hours is exactly what that backstop is for — but the daily and
    intraday bars feed only the prompt, which a skipped pass never builds.
    """
    calls: list[str] = []

    def _bars(broker, symbols):
        calls.append("daily")
        return {}, list(symbols)

    def _intraday(broker, symbols):
        calls.append("intraday")
        return {}, list(symbols)

    def _ticks(broker, symbols):
        calls.append("ticks")
        return {}

    monkeypatch.setattr(main_mod, "fetch_indicators", _bars)
    monkeypatch.setattr(main_mod, "fetch_intraday", _intraday)
    monkeypatch.setattr(main_mod, "fetch_market_ticks", _ticks)

    _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _ExplodingClaude(AssertionError("the model must not be called when shut")),
        in_session=False,
    )
    assert calls == ["ticks"], "a shut market must not pay for bars nobody renders"

    calls.clear()
    _run_one_cycle_with_client(
        monkeypatch,
        tmp_path,
        _StubClaude(ModelDecision(market_assessment="Open.", proposals=[])),
        in_session=True,
    )
    assert calls == ["ticks", "daily", "intraday"], "an open market still needs all three"


def _view(*events: tuple[str, datetime, dict[str, Any]]) -> Any:
    """An `AuditView` built by hand, so a reader test needs no clock and no disk."""
    from bot.audit import AuditView, EventEntry

    view = AuditView()
    view.events = [EventEntry(kind=k, timestamp=t, payload=p) for k, t, p in events]
    return view


def _job_event(
    started: datetime, outcome: str = "ran", *, interval: float | None = 900.0
) -> tuple[str, datetime, dict[str, Any]]:
    payload: dict[str, Any] = {
        "outcome": outcome,
        "started_at": started.isoformat(),
        "interval_seconds": interval,
        "detail": "",
        "proposals": 0 if outcome == "ran" else None,
        "approved": 0 if outcome == "ran" else None,
        "executed": 0 if outcome == "ran" else None,
    }
    # Recorded a second after it began, which is what a real pass looks like.
    return (jobs.JOB_EVENT, started + timedelta(seconds=1), payload)


def test_a_moment_nothing_covers_is_not_recorded_rather_than_quiet(tmp_path):
    """The fourth state. The loop was stopped, restarting, or its record was
    lost — and none of those is "it ran and found nothing to do"."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    history = jobs.history(
        _view(
            _job_event(now - timedelta(minutes=120)),
            _job_event(now - timedelta(minutes=15)),
        ),
        now=now,
    )

    inside = history.at(now - timedelta(minutes=60))
    assert inside.coverage is jobs.Coverage.NOT_RECORDED
    assert inside.job is None
    assert "NOT a report that it ran and did nothing" in inside.describe()

    covered = history.at(now - timedelta(minutes=10))
    assert covered.coverage is jobs.Coverage.FOUND
    assert covered.job is not None and covered.job.outcome is jobs.Outcome.RAN


def test_a_gap_names_whether_a_shutdown_explains_it(tmp_path):
    """A `loop_end` inside the gap means somebody stopped the process, which is
    ordinary. A gap with no shutdown in it is the loop having gone away while it
    was supposed to be running, and only one of those is a fault."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    stopped = jobs.history(
        _view(
            _job_event(now - timedelta(minutes=120)),
            ("loop_end", now - timedelta(minutes=118), {}),
            _job_event(now - timedelta(minutes=15)),
        ),
        now=now,
    )
    assert len(stopped.gaps) == 1
    assert stopped.gaps[0].explained_by_shutdown is True
    assert stopped.gaps[0].missed == 6

    vanished = jobs.history(
        _view(
            _job_event(now - timedelta(minutes=120)),
            _job_event(now - timedelta(minutes=15)),
        ),
        now=now,
    )
    assert len(vanished.gaps) == 1
    assert vanished.gaps[0].explained_by_shutdown is False


def test_consecutive_passes_are_not_reported_as_a_gap(tmp_path):
    """Without grace every pair of passes is a gap and the finding is worthless
    on the first read: a cycle sleeps AFTER its work, so the next one starts at
    interval-plus-duration rather than at exactly the interval."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    history = jobs.history(
        _view(
            _job_event(now - timedelta(minutes=45)),
            _job_event(now - timedelta(minutes=30)),
            _job_event(now - timedelta(minutes=15)),
        ),
        now=now,
    )
    assert history.gaps == []
    assert history.at(now - timedelta(minutes=22)).coverage is jobs.Coverage.FOUND


def test_a_truncated_read_says_it_cannot_answer_rather_than_no(tmp_path):
    """`AuditLog.read` bounds what it returns, so the absence of a pass older
    than the oldest one held says nothing about the loop — it says the read
    stopped. Same trap as `seen.reaches_past_marker`."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    view = _view(_job_event(now - timedelta(minutes=15)))
    older = now - timedelta(hours=6)

    honest = jobs.history(view, now=now, limit_reached=True)
    assert honest.at(older).coverage is jobs.Coverage.OUT_OF_RANGE
    assert honest.is_degraded is True

    complete = jobs.history(view, now=now, limit_reached=False)
    assert complete.at(older).coverage is jobs.Coverage.NOT_RECORDED


def test_an_outcome_this_build_does_not_know_is_counted_not_defaulted(tmp_path):
    """A lenient fallback plus a silent coercion is a mapping that can fail
    completely while looking healthy — which is exactly what `str()` on an SDK
    enum did to every order status on the Board."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    history = jobs.history(
        _view(
            _job_event(now - timedelta(minutes=30), outcome="dreamt"),
            _job_event(now - timedelta(minutes=15)),
        ),
        now=now,
    )
    assert history.unreadable_records == 1
    assert [j.outcome for j in history.jobs] == [jobs.Outcome.RAN]
    assert history.is_degraded is True
    assert any("could not be read" in line for line in jobs.render(history, now=now))


def test_no_passes_on_file_is_not_a_quiet_window(tmp_path):
    """The rule the whole module is built on, at the top level: an empty history
    means nobody looked, never that there was nothing to do."""
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    history = jobs.history(_view(), now=now)

    assert history.has_jobs is False
    lines = jobs.render(history, now=now)
    assert any("not running" in line for line in lines)
    assert not any("stood pat" in line for line in lines)


def test_the_jobs_command_is_dispatched_and_never_falls_through_to_the_loop(
    monkeypatch, tmp_path, capsys
):
    """A command listed in `choices` with no branch of its own does not error —
    the foot of `main()` returns `cmd_loop`, so it silently starts the decision
    loop. That is the worst available failure for a read-only command, and it is
    invisible from reading the argparse block."""
    monkeypatch.setattr(main_mod, "AuditLog", lambda: AuditLog(tmp_path / "audit"))

    def _must_not_run(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("`jobs` must never reach the decision loop")

    monkeypatch.setattr(main_mod, "cmd_loop", _must_not_run)
    monkeypatch.setattr("sys.argv", ["electrum-bot", "jobs"])

    assert main_mod.main() == 0
    out = capsys.readouterr().out
    # Nothing has run here, and the empty answer says which kind of empty it is.
    assert "not running" in out


def test_the_jobs_command_reports_all_three_outcomes(monkeypatch, tmp_path, capsys):
    """The operator's question, answered without a grep: which passes ran, which
    were skipped with the market shut, and which could not get a decision."""
    audit = AuditLog(tmp_path / "audit")
    monkeypatch.setattr(main_mod, "AuditLog", lambda: audit)
    started = datetime.now(UTC) - timedelta(minutes=5)
    standing = jobs.Standing(equity_usd=100_000.0, open_positions=0, open_risk_usd=0.0)

    jobs.record_ran(
        audit, started_at=started, interval_seconds=900.0, standing=standing,
        proposals=0, approved=0, executed=0,
    )
    jobs.record_skipped(
        audit, started_at=started + timedelta(minutes=1), interval_seconds=900.0,
        standing=standing, detail="No enabled instrument class was in session.",
    )
    jobs.record_failed(
        audit, started_at=started + timedelta(minutes=2), interval_seconds=900.0,
        standing=standing, detail="TimeoutError: read timed out",
    )

    assert main_mod.cmd_jobs(hours=24.0, at=None) == 0
    out = capsys.readouterr().out
    assert "1 ran, 1 skipped with the market shut, 1 could not get a decision" in out
    assert "read timed out" in out

    # And a moment nobody recorded is answered as such rather than as a hold.
    assert main_mod.cmd_jobs(hours=24.0, at="2020-01-01T14:15") == 0
    assert "cannot be established" in capsys.readouterr().out

    # A time nobody can parse is refused rather than silently answered about now.
    assert main_mod.cmd_jobs(hours=24.0, at="yesterday afternoon") == 2
    assert "Could not read" in capsys.readouterr().out


def test_a_pass_that_stated_no_cadence_says_so_instead_of_claiming_a_gap(tmp_path):
    """A record with no `interval_seconds` cannot be stretched over a window
    somebody assumed. The honest answer names the pass and says its reach is
    unknown, rather than reporting the loop as absent when one is sitting there.
    """
    now = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
    history = jobs.history(
        _view(_job_event(now - timedelta(minutes=40), interval=None)), now=now
    )

    answer = history.at(now - timedelta(minutes=5))
    assert answer.coverage is jobs.Coverage.NOT_RECORDED
    assert answer.nearest_before is not None
    assert "did not state its cadence" in answer.describe()
    # And it is not counted as a gap either, for the same reason.
    assert history.gaps == []


# ------------------------------------------- which credential, and who refuses
#
# `smoketest`, `dream` and `confer` each asked whether ANTHROPIC_API_KEY was
# set. With two possible endpoints that question is wrong in both directions,
# and the loop asks a deliberately narrower one. These pin the asymmetry,
# because it looks like an inconsistency and is the opposite of one.


def _provider_env(**overrides: Any) -> Any:
    from bot.config import Env

    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.anthropic_api_key = ""
    env.do_inference_key = ""
    env.do_inference_base_url = ""
    # Cleared for the same reason the three above are: whatever is in the
    # environment of the machine running the suite must not decide which
    # configuration a test is describing.
    env.decision_model_id = ""
    env.dream_model_id = ""
    for key, value in overrides.items():
        setattr(env, key, value)
    return env


def test_a_missing_key_is_reported_against_the_provider_that_would_read_it():
    """The guard that said yes about a credential nothing was going to use.

    Both directions are checked, and the second is the one that hurts.

    A DigitalOcean box with no Anthropic key used to REFUSE, naming a variable
    the operator had deliberately not set. That is loud and merely annoying.

    The quiet one is a half-finished swap: the endpoint set, the model access
    key not. The old check asked about `ANTHROPIC_API_KEY`, found it, and said
    everything was fine — while `inference_provider` was reporting in as many
    words that nothing had moved. A guard that passes over a configuration the
    operator believes is live is worse than no guard.
    """
    on_anthropic = _provider_env()
    blocked = main_mod.model_calls_are_impossible(on_anthropic)
    assert blocked and "ANTHROPIC_API_KEY" in blocked

    half_a_swap = _provider_env(
        anthropic_api_key="anthropic-key",
        do_inference_base_url="https://inference.do-ai.run",
    )
    assert half_a_swap.anthropic_api_key  # what the old check looked at, and found
    blocked = main_mod.model_calls_are_impossible(half_a_swap)
    assert blocked and "nothing has moved" in blocked

    # A model has to be NAMED, and this line used to omit it and still assert
    # the configuration was fine. That is the hole audit found: with no
    # `DECISION_MODEL_ID` the spec falls back to a Claude tier id, which
    # `ModelClient.__init__` refuses outright — so the guard said yes to a
    # configuration whose very next statement raises. See
    # `test_the_loop_refuses_exactly_what_the_client_refuses`.
    configured = _provider_env(
        do_inference_key="do-model-access-key",
        decision_model_id="llama3.3-70b-instruct",
    )
    assert main_mod.model_calls_are_impossible(configured) is None
    # And the Anthropic key is neither required nor consulted once it is.
    assert not configured.anthropic_api_key


def test_an_unusable_provider_is_reported_even_with_a_key_present():
    """Configured and wrong is not the same fault as incomplete.

    `usable=False` means the operator set something that cannot work, and no key
    makes that better. Reporting it as "no credential" would send them to create
    a second key for a configuration that would still refuse.
    """
    env = _provider_env(
        do_inference_key="do-model-access-key",
        do_inference_base_url="http://inference.do-ai.run",  # not https
    )

    assert env.model_api_key  # the key IS there
    blocked = main_mod.model_calls_are_impossible(env)
    assert blocked and "NOT a fall back to Anthropic" in blocked
    assert main_mod.provider_is_unusable(env) is not None


def test_the_loop_refuses_a_wrong_endpoint_and_tolerates_a_missing_key():
    """The asymmetry, and it is deliberate rather than an oversight.

    A cycle with no model call still reconciles the journal against the broker
    and still runs `stop_watch` over open positions. That safety work is worth
    more than the proposal, so a missing key degrades the cycle exactly as a
    dead feed does — which is also why every loop test in this file runs with no
    key at all and a stub client.

    A provider that is configured and WRONG is the other case. `ModelClient`
    refuses to construct against it, once, at loop start, outside the per-cycle
    `except` that turns a bad call into a skipped cycle. Uncaught that is a
    traceback, and under systemd a traceback is a restart into the identical
    failure — the shape `CLAUDE.md` records as costing a live cycle once
    already.
    """
    no_key = _provider_env()
    assert main_mod.provider_is_unusable(no_key) is None
    assert main_mod.model_calls_are_impossible(no_key) is not None

    wrong_endpoint = _provider_env(
        do_inference_key="do-model-access-key",
        do_inference_base_url="http://inference.do-ai.run",
    )
    assert main_mod.provider_is_unusable(wrong_endpoint) is not None


#: Every configuration the two guards are asked about, as
#: `(name, env kwargs)`. Driven against the REAL `ModelClient` below, so the
#: table is a description of the world rather than of the guard.
_PROVIDER_CONFIGURATIONS: list[tuple[str, dict[str, Any]]] = [
    ("nothing set at all", {}),
    ("anthropic key only", {"anthropic_api_key": "ak"}),
    (
        "DO key, model named",
        {"do_inference_key": "dk", "decision_model_id": "llama3.3-70b-instruct"},
    ),
    # The hole. The key is the switch an operator is told to set; the model id
    # is a second variable and is easy to miss.
    ("DO key, NO model named", {"do_inference_key": "dk"}),
    (
        "DO key, model named, base url not https",
        {
            "do_inference_key": "dk",
            "do_inference_base_url": "http://inference.do-ai.run",
            "decision_model_id": "llama3.3-70b-instruct",
        },
    ),
    ("DO key, no model, base url not https",
     {"do_inference_key": "dk", "do_inference_base_url": "http://inference.do-ai.run"}),
    # The mirror hole: a configuration `.env.example` describes as moving
    # nothing, which the loop refused outright.
    (
        "DO base url set, no DO key, anthropic key present",
        {"anthropic_api_key": "ak", "do_inference_base_url": "https://inference.do-ai.run"},
    ),
    (
        "DO base url set, no DO key, no anthropic key",
        {"do_inference_base_url": "https://inference.do-ai.run"},
    ),
]


def test_the_loop_refuses_exactly_what_the_client_refuses():
    """**The anti-drift lock, and it is the whole reason the guard may exist.**

    `provider_is_unusable` is a second copy of `ModelClient.__init__`'s refusal
    list living in another file, and a second copy is a second thing to be
    wrong. It was wrong in BOTH directions at once, which is what this pins:

    - It cleared `DO_INFERENCE_KEY` with no `DECISION_MODEL_ID`, and the loop
      then raised `ModelCallFailed` out of `cmd_loop` — a traceback under
      systemd, restarted into forever, which is the exact failure the guard was
      added to prevent.
    - It refused `DO_INFERENCE_BASE_URL` set without the key, where the client
      builds happily and the calls go to Anthropic. `cmd_loop` exited 1, taking
      `reconcile`, `stop_watch` and the expiry alerts down with it.

    So this drives the REAL constructor over every configuration and asserts the
    two agree. A third refusal added to `ModelClient` turns this red rather than
    reopening the traceback.
    """
    from bot.model_client import ModelCallFailed, ModelClient

    for name, overrides in _PROVIDER_CONFIGURATIONS:
        env = _provider_env(**overrides)
        try:
            ModelClient(env, "system prompt")
        except ModelCallFailed:
            client_refused = True
        else:
            client_refused = False

        assert bool(main_mod.provider_is_unusable(env)) is client_refused, name
        # And the wider question can never be softer than the narrower one: a
        # command that cannot construct a client cannot make a call.
        if client_refused:
            assert main_mod.model_calls_are_impossible(env), name


def test_the_dreamer_is_guarded_against_its_OWN_model_not_the_loops():
    """`DREAM_MODEL_ID` and `DECISION_MODEL_ID` are two variables.

    `Dreamer.__init__` and `Conference._build` both construct with
    `env.dream_spec`, outside any `except`. A guard asked about the decision
    model would clear a box where the loop's model is named and the dreamer's is
    not, and hand `electrum-bot dream` the traceback instead.
    """
    from bot.model_client import ModelCallFailed, ModelClient

    env = _provider_env(
        do_inference_key="dk",
        decision_model_id="llama3.3-70b-instruct",  # named
        dream_model_id="",                          # and not
    )

    assert main_mod.model_calls_are_impossible(env) is None
    blocked = main_mod.model_calls_are_impossible(env, spec=env.dream_spec)
    assert blocked and "DREAM_MODEL_ID" in blocked
    with pytest.raises(ModelCallFailed):
        ModelClient(env, "system prompt", spec=env.dream_spec)


def test_the_refusal_never_prints_the_key():
    """Credentials are reported as configured or not configured, never rendered.

    This sentence goes to the journal and to `systemctl --failed`, both of which
    outlive the process and neither of which is private.
    """
    env = _provider_env(
        do_inference_key="do-secret-value",
        do_inference_base_url="http://inference.do-ai.run",
    )

    assert "do-secret-value" not in (main_mod.model_calls_are_impossible(env) or "")


# --------------------------- a decision that considered nothing at all
#
# The second entrance to the quiet-cycle hole. A reply with NO tool call already
# fails hard in `ModelClient`; a tool call that IS made and comes back with
# `assessments: []` used to arrive at exactly the same place and be recorded as
# a considered decision. `llama3.3-70b-instruct` does that on 6 of 10 samples
# after being shown four symbols and an open position, in a 2.2-second median
# (`docs/DROPLET_AI.md`). Structurally valid, passes Pydantic, and it puts back
# the whole gap `assessments` exists to close.
#
# The rule these pin is a RATIO, which is why it can only live in `cmd_loop`:
# neither `ModelDecision` nor `ModelClient` knows how many symbols were shown.


def _bars(symbol: str, count: int = 30, start: float = 100.0) -> list[Any]:
    """Enough daily history for `indicators.compute` to return something.

    The figures are unimportant — what matters is that the symbol lands in the
    INDICATOR set rather than in `symbols_without_history`, because that set is
    the denominator the refusal is measured against.
    """
    from bot.models import Bar

    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i),
            open=start + i,
            high=start + i + 1.0,
            low=start + i - 1.0,
            close=start + i,
            volume=1_000_000.0,
        )
        for i in range(count)
    ]


def _broker_showing(*symbols: str) -> Any:
    """A broker with daily bars for exactly these symbols and nothing else.

    Every other name in `allowed_symbols` raises out of `get_daily_bars`, which
    `fetch_indicators` reports as missing history — so the indicator set is
    exactly what this names, and the test can say what the model was shown.
    """
    from bot.broker import MockBroker

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    for symbol in symbols:
        broker.set_price(symbol, bid=99.98, ask=100.02)
        broker.set_bars(symbol, _bars(symbol))
    return broker


def _assessed(symbol: str) -> Any:
    from bot.models import Stance, SymbolAssessment

    return SymbolAssessment(
        symbol=symbol,
        stance=Stance.PASS,
        reasoning=f"{symbol} is mid-range with no edge worth taking today.",
    )


def test_a_decision_assessing_none_of_the_symbols_shown_fails_the_cycle(
    monkeypatch, tmp_path
):
    """The REJECT. Two symbols with indicators, zero assessments, cycle refused.

    This is the `llama3.3-70b-instruct` answer exactly: valid against the
    schema, instant, and empty. Recorded, it is indistinguishable afterwards
    from a loop that never looked at either symbol — which is the one state
    `assessments` exists to make impossible.

    It takes the identical path to a timeout: `model_call_failed`, a failed job
    record, and **no `cycle_complete`**. A cycle that could not get a usable
    decision must never be readable as one that decided to do nothing.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Mixed tape.", proposals=[], assessments=[]),
        _broker_showing("SPY", "QQQ"),
    )

    failures = [e for e in logs if e["event"] == "model_call_failed"]
    assert len(failures) == 1
    # The message names what it was shown, so the record says WHY it refused
    # rather than only that it did.
    assert "SPY" in failures[0]["error"] and "QQQ" in failures[0]["error"]
    assert "ConsideredNothing" in failures[0]["error"]
    assert not [e for e in logs if e["event"] == "cycle_complete"]


def test_a_cycle_that_showed_no_symbols_is_quiet_rather_than_failed(
    monkeypatch, tmp_path
):
    """The other half, and the one that would break a correct bot if it were
    wrong.

    Nothing was seeded, so every symbol raises out of `get_daily_bars` and lands
    in `symbols_without_history`. The model was given no indicators at all, so
    an empty assessment list is the only honest answer it could return — and
    refusing it would turn a data outage, or a shut market, into a failed cycle
    every fifteen minutes.
    """
    from bot.broker import MockBroker

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()

    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Nothing to read.", proposals=[], assessments=[]),
        broker,
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    beat = _heartbeat(logs)
    assert beat["symbols_shown"] == 0
    assert beat["assessments"] == 0


def test_a_quote_without_history_is_not_counted_as_a_symbol_shown(
    monkeypatch, tmp_path
):
    """The denominator is the INDICATOR set, not the quotes and not the allowlist.

    A symbol with a live quote and no bars is one the context tells the model to
    treat as having no indicators at all and to propose nothing on. Counting it
    would fail the cycle for obeying that instruction, and it would blame the
    model for a feed that was down.
    """
    from bot.broker import MockBroker

    broker = MockBroker(starting_equity=100_000.0)
    broker.connect()
    broker.set_price("SPY", bid=99.98, ask=100.02)  # a quote, and no bars

    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quote only.", proposals=[], assessments=[]),
        broker,
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    beat = _heartbeat(logs)
    assert beat["symbols_shown"] == 0
    assert "SPY" in beat["symbols_without_history"]


def test_a_partial_answer_is_not_refused_and_the_ratio_is_stated(monkeypatch, tmp_path):
    """Zero is the trip, not a shortfall — and the shortfall is made visible.

    A model that assessed one of two symbols has made a judgement a reader can
    see and argue with on the Decisions page. A model that assessed none has
    produced a record indistinguishable from never having looked. Only the
    second is a failed cycle.

    Because the first is deliberately allowed through, the ratio goes on the
    cycle line: without it, one of two reads exactly like two of two in every
    other field.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Looked at one of them properly.",
            proposals=[],
            assessments=[_assessed("SPY")],
        ),
        _broker_showing("SPY", "QQQ"),
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    beat = _heartbeat(logs)
    assert beat["symbols_shown"] == 2
    assert beat["assessments"] == 1
    # Nothing strayed, so the count above genuinely is one of two.
    assert beat["assessments_off_list"] == []


def test_a_decision_that_assessed_only_symbols_it_was_never_shown_is_refused(
    monkeypatch, tmp_path
):
    """**Assessing NONE of what you were shown, with a non-empty list.**

    The refusal counted the list's LENGTH while every sentence around it — the
    docstring, the message it raises, the field on the cycle line — described a
    RATIO against the indicator set. Reproduced: two symbols shown, two
    assessments naming neither, cycle recorded, and `cycle_complete` read
    `symbols_shown=2, assessments=2`. The one field put there to make a partial
    answer visible reported a complete one, which is worse than the gap it was
    covering.

    This is reachable without inventing a hallucination. `SymbolAssessment.symbol`
    is free text with no allowlist behind it, and the context block ASKS for
    symbols outside the current indicator set: "What you said last cycle" lists
    the previous cycle's assessments and the prompt tells the model to report on
    each of them. A symbol whose bars failed this cycle, or whose dream grant
    lapsed, is in that list and not in the indicator set — so a model that
    answers only the recall section lands here, and looks diligent doing it.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Mixed tape.",
            proposals=[],
            assessments=[_assessed("BANANA"), _assessed("TSLA")],
        ),
        _broker_showing("SPY", "QQQ"),
    )

    failures = [e for e in logs if e["event"] == "model_call_failed"]
    assert len(failures) == 1
    assert "ConsideredNothing" in failures[0]["error"]
    # The message says WHICH question was answered instead, or an operator
    # reading "assessed none of them" beside a list of two would think the
    # record was lying to them.
    assert "BANANA" in failures[0]["error"] and "TSLA" in failures[0]["error"]
    assert not [e for e in logs if e["event"] == "cycle_complete"]


def test_a_stray_assessment_is_named_rather_than_padding_the_ratio(
    monkeypatch, tmp_path
):
    """One real assessment plus one stray is NOT refused, and is not 2 of 2.

    A stray is frequently the model doing as it was told — the recall block asks
    for a report on last cycle's symbols, some of which may have dropped out of
    the indicator set since. So this is the repository's standing rule: report
    the gap, do not refuse. What must not happen is the stray quietly counting
    towards the ratio, which is what turns "one of the two symbols was never
    mentioned" into a line that reads as a full answer.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Reported on the watch and one live name.",
            proposals=[],
            assessments=[_assessed("SPY"), _assessed("BANANA")],
        ),
        _broker_showing("SPY", "QQQ"),
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    beat = _heartbeat(logs)
    assert beat["symbols_shown"] == 2
    assert beat["assessments"] == 2
    assert beat["assessments_off_list"] == ["BANANA"]


def test_a_lower_case_symbol_is_not_a_stray(monkeypatch, tmp_path):
    """The false refusal the fix must not introduce.

    `SymbolAssessment.symbol` is free text and nothing upper-cases it, so a
    model answering `spy` for `SPY` has assessed what it was shown. Refusing a
    correct answer over its casing would fail a cycle that was fine — the one
    outcome worse than the gap being open.
    """
    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Case is not a stance.",
            proposals=[],
            assessments=[_assessed("spy")],
        ),
        _broker_showing("SPY", "QQQ"),
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    assert _heartbeat(logs)["assessments_off_list"] == []


def test_an_open_position_with_no_plan_is_reported_and_never_fails_the_cycle(
    monkeypatch, tmp_path
):
    """The same shape of defect, deliberately answered differently.

    `position_plans` empty against an open position is the other half of what
    the fidelity probe grades as `empty_arrays`, and it is REPORTED rather than
    refused. An unassessed symbol is unrecoverable — the audit log is the only
    place it was ever written down. An unplanned position is not: it is in the
    journal, on the Board, in `reconcile`, in `stop_watch` and behind a resting
    stop leg, and the plan is advisory and never executed unless the operator
    switched position actions on.

    Refusing it would also throw away good work — a decision can carry sound
    assessments and a sound proposal while saying nothing about a held
    position.
    """
    broker = _short_spy_at_the_broker()
    broker.set_bars("SPY", _bars("SPY", start=780.0))

    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Short is fine, nothing new to add about it.",
            proposals=[],
            assessments=[_assessed("SPY")],
            position_plans=[],
        ),
        broker,
        journal=_journal_with_the_short(tmp_path),
    )

    assert not [e for e in logs if e["event"] == "model_call_failed"]
    beat = _heartbeat(logs)
    assert beat["positions_without_a_plan"] == ["SPY"]
    # Stated where an operator is looking, not only counted.
    gaps = [e for e in logs if e["event"] == "positions_without_a_plan"]
    assert len(gaps) == 1 and gaps[0]["symbols"] == ["SPY"]


def test_a_planned_position_leaves_the_field_empty_rather_than_absent(
    monkeypatch, tmp_path
):
    """A stated empty list each cycle is a fact; a field that only appears when
    something is wrong makes an outage look like a clean run. Same reasoning as
    `stops_breached`."""
    from bot.models import PositionAction, PositionPlan

    broker = _short_spy_at_the_broker()
    broker.set_bars("SPY", _bars("SPY", start=780.0))

    logs = _cycle_with_broker(
        monkeypatch,
        tmp_path,
        ModelDecision(
            market_assessment="Short is fine.",
            proposals=[],
            assessments=[_assessed("SPY")],
            position_plans=[
                PositionPlan(
                    symbol="SPY",
                    action=PositionAction.HOLD,
                    reasoning="Thesis intact; the stop is where it was sized.",
                )
            ],
        ),
        broker,
        journal=_journal_with_the_short(tmp_path),
    )

    beat = _heartbeat(logs)
    assert beat["positions_without_a_plan"] == []
    assert not [e for e in logs if e["event"] == "positions_without_a_plan"]


def test_the_refusal_is_a_model_call_failure_rather_than_a_new_kind_of_error():
    """Raised, not branched on, and in the family every caller already handles.

    `ModelCallFailed` is the base class `cmd_loop` turns into a skipped cycle.
    A refusal outside that family would need its own handler, and the two would
    drift the first time one of them changed.
    """
    from bot.model_client import ModelCallFailed

    assert issubclass(main_mod.ConsideredNothing, ModelCallFailed)

    shown = ["QQQ", "SPY"]
    empty = ModelDecision(market_assessment="Quiet.", proposals=[], assessments=[])
    with pytest.raises(ModelCallFailed) as caught:
        main_mod.refuse_a_decision_that_considered_nothing(empty, shown=shown)
    # Both symbols named: the record has to say what it was measured against.
    assert "QQQ, SPY" in str(caught.value)

    # And neither the empty denominator nor a non-empty answer raises.
    main_mod.refuse_a_decision_that_considered_nothing(empty, shown=[])
    main_mod.refuse_a_decision_that_considered_nothing(
        ModelDecision(
            market_assessment="Quiet.", proposals=[], assessments=[_assessed("SPY")]
        ),
        shown=shown,
    )


def test_the_heartbeat_names_the_model_that_actually_answered(monkeypatch, tmp_path):
    """A substitution is invisible everywhere else on a successful cycle.

    The call succeeds, the schema validates, and the cost is computed from the
    price sheet of the model that was ASKED for — so a catalogue aliasing an id
    to a successor, or a proxy named in `ANTHROPIC_BASE_URL` routing elsewhere,
    produces a completely ordinary-looking cycle whose orders were sized by
    weights nobody named. `CLAUDE.md` records the endpoint-versus-configuration
    version of this mistake already; this is the same one a level in.
    """
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quiet.", proposals=[], assessments=[]),
    )
    beat = _heartbeat(logs)
    assert beat["served_model"] == "claude-haiku-4-5-20251001"
    assert beat["served_as_requested"] is True


def test_a_model_that_did_not_answer_its_own_name_is_reported_not_refused(
    monkeypatch, tmp_path
):
    """Reported. The cycle completes, and that is the deliberate half.

    An alias is an ordinary thing for a catalogue to do. Throwing away a
    validated decision — its proposals, its assessments and the cost already
    spent — over a naming difference would cost far more than it protects, so
    the mismatch goes on the pulse and into the audit record and the loop
    carries on.
    """

    class _Substituted(_StubClaude):
        served = "anthropic-claude-5-haiku"

    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quiet.", proposals=[], assessments=[]),
        client_cls=_Substituted,
    )
    beat = _heartbeat(logs)
    assert beat["served_model"] == "anthropic-claude-5-haiku"
    assert beat["served_as_requested"] is False


def test_a_response_carrying_no_model_id_reads_as_unknown_on_the_pulse(
    monkeypatch, tmp_path
):
    """`None` is "could not ask", and it must not read as agreement.

    The good outcome must not be what an absence of evidence looks like — the
    same rule that makes the tailnet status report `unknown` rather than
    healthy when the check itself has stopped.
    """

    class _Anonymous(_StubClaude):
        served = ""

    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quiet.", proposals=[], assessments=[]),
        client_cls=_Anonymous,
    )
    beat = _heartbeat(logs)
    assert beat["served_model"] == ""
    assert beat["served_as_requested"] is None


def test_the_heartbeat_states_tight_and_unmeasured_stops_as_two_facts(
    monkeypatch, tmp_path
):
    """`stops_unchecked` one column across, on a line that already carries it.

    A cycle with no proposals states two empty lists rather than staying
    silent, because an absent field is what an outage looks like — the same
    reason `stops_breached` reports a zero every cycle.
    """
    logs = _run_one_cycle(
        monkeypatch,
        tmp_path,
        ModelDecision(market_assessment="Quiet.", proposals=[], assessments=[]),
    )
    beat = _heartbeat(logs)
    assert beat["tight_stops"] == []
    assert beat["stops_unmeasured"] == []
