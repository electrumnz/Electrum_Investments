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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

import bot.main as main_mod
from bot.audit import AuditLog
from bot.claude_client import CallUsage, ClaudeDecision
from bot.confer import CONFERENCE
from bot.config import Env, Rules, load_rules
from bot.dreaming import DreamStore, Hop, Vault
from bot.journal import Journal
from bot.models import Decision, IndicatorSnapshot, MarketInputs


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
        ClaudeDecision(market_assessment="Quiet, and nothing adopted.", proposals=[]),
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
        ClaudeDecision(market_assessment="One dream adopted.", proposals=[]),
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
        ClaudeDecision(market_assessment="One dream adopted.", proposals=[]),
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
        ClaudeDecision(market_assessment="One dream adopted.", proposals=[]),
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
