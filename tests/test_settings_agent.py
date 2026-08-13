"""The settings agent: the argument, the record, and the privileged apply.

Two things matter here and they are different from the rest of the suite.

**The argument has to be substantive.** A test that only proved the agent said
something would pass over a mood. So the assertions are on the arithmetic —
that doubling a per-trade cap is reported as doubling the loss on every trade,
that two trades then exhaust the portfolio total, that a class limit above the
total can never fill — because that is the part that earns the feature.

**Nothing here may write `config/rules.yaml`.** Every test copies it into a
`tmp_path` first. The repository's own file is read for its shape and never
edited, which is also the production arrangement: `config/` is root-owned on the
box so the service account cannot edit its own limits.

No model is involved in any of this. The Armorer's soul shapes how the argument
is said; everything asserted below is deterministic Python, which is the whole
reason the consequence can be trusted.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bot.config import DEFAULT_RULES_PATH, Rules
from bot.settings_agent import (
    DirectApplier,
    Looser,
    SettingsRequestStore,
    Stance,
    Unit,
    UnknownLimit,
    WrapperApplier,
    apply_request,
    argue,
    build_request,
    commit_message,
    decide,
    digest,
    effective_value,
    limits_for,
    locate,
    render_briefing,
    render_diff,
    revert_request,
)

EQUITY = 100_000.0

#: The wrapper script, as the suite sees it.
#:
#: **No test in this file may invoke `sudo` or run the real wrapper against the
#: real `config/rules.yaml`.** Every applier below either has its subprocess
#: stubbed at the boundary or writes to a `tmp_path` copy. A test that depended
#: on `/opt/mudhorn` being absent would pass for a reason that has nothing to do
#: with the code, and would start editing a live risk limit the day somebody ran
#: the suite on the droplet.
WRAPPER_SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "apply-settings.sh"


@pytest.fixture
def rules_file(tmp_path) -> Path:
    """A copy of the shipped rules. Never the real file, in any test."""
    target = Path(tmp_path) / "rules.yaml"
    shutil.copy(DEFAULT_RULES_PATH, target)
    return target


@pytest.fixture
def rules(rules_file) -> Rules:
    return Rules.load(rules_file)


@pytest.fixture
def store(tmp_path) -> SettingsRequestStore:
    return SettingsRequestStore(tmp_path / "settings_requests.db")


def _always_available() -> WrapperApplier:
    """An applier whose wrapper is this file, which certainly exists.

    Used only where the QUESTION is "what does the surface say when the
    privileged route is there" — nothing is ever run through it.
    """
    return WrapperApplier(wrapper=Path(__file__), sudo="")


def _stub_wrapper(
    store: SettingsRequestStore, rules_file: Path, tmp_path: Path
) -> tuple[WrapperApplier, list[tuple[list[str], str]]]:
    """A `WrapperApplier` stubbed at the process boundary, and its call log.

    The real argv construction, the real stdin string and the real return-code
    handling all run; only `subprocess.run` is replaced, by something that does
    what the wrapper would have done — call the same `apply_request` the CLI
    calls, against a `tmp_path` copy of the rules.

    That seam is the right one. Shelling out to the real script would need sudo
    and a provisioned box, and stubbing any higher would stop testing the thing
    the sudoers rule actually points at: what goes down stdin.
    """
    script = tmp_path / "apply-settings.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    calls: list[tuple[list[str], str]] = []

    def runner(argv: list[str], stdin: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((argv, stdin))
        verb, _, raw = stdin.strip().partition(" ")
        run = revert_request if verb == "revert" else apply_request
        result = run(store, int(raw), rules_path=rules_file, actor="stub")
        return subprocess.CompletedProcess(
            argv,
            0 if result.ok else 1,
            result.detail,
            "" if result.ok else result.detail,
        )

    return WrapperApplier(wrapper=script, sudo="", runner=runner), calls


# ------------------------------------------------------------------- the table


def test_every_limit_the_agent_argues_about_has_a_consequence(rules):
    """A limit in the table with nothing to say about it would be a limit the
    agent waved through, which is worse than one it never offered."""
    for key, fact in limits_for(rules).items():
        assert fact.what.strip(), key
        assert fact.why.strip(), key
        assert fact.goal.strip(), key
        assert fact.cost.strip(), key
        assert fact.looser in (Looser.HIGHER, Looser.LOWER), key


def test_the_per_class_limits_are_derived_from_the_config(rules):
    """Adding a class to `config/rules.yaml` gives the agent its limits with no
    second edit. Crypto is configured while DISABLED on purpose, and its limits
    are exactly what somebody would want to argue about before enabling it."""
    keys = set(limits_for(rules))

    assert "instruments.crypto.max_risk_per_trade_pct" in keys
    assert "instruments.us_equity.max_risk_per_trade_pct" in keys
    assert not rules.instruments["crypto"].enabled


def test_every_requestable_limit_can_be_found_in_the_real_file(rules_file, rules):
    """The table and the file have to agree, and only the file can say so.

    A fact naming a key that is not in `config/rules.yaml` would produce an
    argument the operator could win and then not be able to act on.
    """
    text = rules_file.read_text()
    missing = [
        key
        for key, fact in limits_for(rules).items()
        if fact.requestable and locate(text, key) is None
    ]

    # `max_class_total_risk_pct` on the equity class is genuinely absent — the
    # class has no class total and is bounded by the portfolio one alone. That
    # is a real state, not an oversight, and the agent reports it as unset
    # rather than as an inherited figure.
    assert missing == ["instruments.us_equity.max_class_total_risk_pct"]


def test_the_same_limit_name_in_three_blocks_resolves_to_three_lines(rules_file):
    """`max_risk_per_trade_pct` appears under `account:` and under each
    instrument class. A regex for the name alone would edit whichever came
    first, which is the entire reason the locator tracks indentation."""
    text = rules_file.read_text()

    account = locate(text, "account.max_risk_per_trade_pct")
    equity = locate(text, "instruments.us_equity.max_risk_per_trade_pct")
    crypto = locate(text, "instruments.crypto.max_risk_per_trade_pct")

    assert account and equity and crypto
    assert len({account.line_no, equity.line_no, crypto.line_no}) == 3
    assert crypto.value_text == "0.5"


def test_a_commented_out_template_is_not_configuration(rules_file):
    """`config/rules.yaml` carries a worked CME Globex block, fully commented.

    Reading it as configuration would let the agent offer to edit a class that
    does not exist and cannot: Alpaca offers equities, options and crypto, and
    nothing on Globex.
    """
    text = rules_file.read_text()

    assert "futures:" in text  # the template really is in the file
    assert locate(text, "instruments.futures.capital_cap_pct") is None


def test_an_absent_override_is_unset_and_never_zero(rules):
    """Absent is not zero and it is not "no limit". `us_equity` sets no class
    total, so it is bounded by the portfolio total and by nothing of its own."""
    value, source = effective_value(rules, "instruments.us_equity.max_class_total_risk_pct")

    assert value is None
    assert source == "unset"


# ----------------------------------------------------------- the asymmetry


def test_loosening_states_the_consequence_and_records_nothing_yet(rules, rules_file):
    """The whole asymmetry, and it is in the code rather than only in the voice."""
    argument = argue(
        rules,
        "account.max_risk_per_trade_pct",
        "2.0",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )

    assert argument.stance is Stance.LOOSENING
    assert argument.requires_confirmation
    assert argument.objection.strip()


def test_tightening_is_acknowledged_rather_than_argued(rules, rules_file):
    """Somebody choosing to lose less should not be interrogated for it."""
    argument = argue(
        rules,
        "account.max_risk_per_trade_pct",
        "0.5",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )

    assert argument.stance is Stance.TIGHTENING
    assert not argument.requires_confirmation
    assert argument.objection == ""


def test_the_looser_direction_is_not_guessed_from_the_name(rules, rules_file):
    """`min_equity_floor_usd` loosens DOWNWARD. Getting that backwards would
    make the agent argue hardest against the operator being careful, which is
    the exact inversion the asymmetry exists to avoid."""
    lower = argue(rules, "account.min_equity_floor_usd", "50000", rules_path=rules_file)
    higher = argue(rules, "account.min_equity_floor_usd", "95000", rules_path=rules_file)

    assert lower.stance is Stance.LOOSENING
    assert higher.stance is Stance.TIGHTENING


def test_an_unchanged_value_is_neither_and_records_nothing(rules, rules_file):
    argument = argue(rules, "account.max_total_risk_pct", "2.0", rules_path=rules_file)

    assert argument.stance is Stance.UNCHANGED
    assert not argument.can_record


# ------------------------------------------------------- the real arithmetic


def test_doubling_the_per_trade_cap_is_reported_as_doubling_the_loss(rules, rules_file):
    """The headline consequence, in figures rather than in a mood.

    And the second half is the one an operator does not expect: at 2% a trade
    against a 2% portfolio total, ONE full-size trade fits. The second is
    refused however good it looks.
    """
    argument = argue(
        rules,
        "account.max_risk_per_trade_pct",
        "2.0",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )
    said = " ".join(argument.consequence)

    assert "2.00x the loss" in said
    assert "$2,000.00 instead of $1,000.00" in said
    assert "1 full-size trade fits" in said
    assert "refused however good it looks" in said
    # Size follows stop distance, so a looser cap buys a bigger position at the
    # same stop rather than a safer trade. The prompt says the same thing to the
    # model for the same reason.
    assert "does not buy a wider stop" in said


def test_raising_a_class_limit_above_the_total_is_named_as_unfillable(rules, rules_file):
    """`account:` is the default, not a ceiling — nothing refuses a looser class
    limit. But the portfolio total-risk gate refuses the TRADE, so the change
    does nothing until the total moves too. That is a fact worth telling
    somebody before they make it, not after."""
    argument = argue(
        rules,
        "instruments.crypto.max_risk_per_trade_pct",
        "3.0",
        rules_path=rules_file,
    )
    said = " ".join(argument.consequence)

    assert "could ever fill" in said
    assert "does nothing until the total moves too" in said
    assert "OVERRIDES the portfolio value" in said


def test_loosening_the_stand_down_is_costed_in_equity(rules, rules_file):
    """Loosening this during a losing run is the moment the rule exists for, and
    the argument for it has to be a figure rather than a warning."""
    argument = argue(
        rules,
        "stand_down.consecutive_losses_trigger",
        "6",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )
    said = " ".join(argument.consequence)

    assert "6 qualifying losses in a row" in said
    assert "6.00% of equity lost before the breaker trips" in said
    assert "$6,000.00" in said


def test_lowering_the_equity_floor_is_costed_as_a_drawdown(rules, rules_file):
    argument = argue(
        rules,
        "account.min_equity_floor_usd",
        "50000",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )
    said = " ".join(argument.consequence)

    assert "50.0% drawdown permitted instead of 10.0%" in said
    assert "turns a drawdown into a wipe-out" in said


def test_more_concurrent_positions_is_measured_against_the_total(rules, rules_file):
    """Three slots at 1% is 3% of equity at risk against a 2% total, so the
    total binds first at two. A count that quietly cannot be reached is a
    setting that looks like it does something."""
    argument = argue(
        rules, "account.max_concurrent_positions", "6", rules_path=rules_file
    )
    said = " ".join(argument.consequence)

    assert "6.00% of equity at risk" in said
    assert "binds first at 2" in said


def test_a_missing_equity_reading_is_stated_and_never_assumed(rules, rules_file):
    """No assumed $100,000. A figure that looks like a measurement and is not is
    the thing this repository refuses everywhere else."""
    argument = argue(
        rules, "account.max_risk_per_trade_pct", "2.0", rules_path=rules_file
    )
    said = " ".join(argument.consequence)

    assert "unknown rather than estimated" in said
    # Not a dollar figure anywhere: the percentages stand on their own, and an
    # assumed balance is how a plausible wrong number gets in front of somebody
    # deciding what to risk.
    assert "$" not in said


def test_a_stale_trailing_comment_is_named_rather_than_rewritten(rules, rules_file):
    """`min_seconds_between_trades_per_symbol: 1800   # 30 minutes`.

    A one-line diff cannot know what the prose meant, and rewriting it would be
    guessing. Leaving it silently is worse: a comment that contradicts its own
    value is read as the truth by the next person along.
    """
    argument = argue(
        rules,
        "frequency.min_seconds_between_trades_per_symbol",
        "300",
        rules_path=rules_file,
    )
    said = " ".join(argument.consequence)

    assert "30 minutes" in said
    assert "will describe the old value afterwards" in said


def test_a_value_that_is_not_a_number_is_refused(rules, rules_file):
    """Prose truncates; numbers reject. A plausible wrong figure that passes
    validation is the exact failure this repository exists to prevent."""
    with pytest.raises(ValueError):
        argue(rules, "account.max_risk_per_trade_pct", "about two", rules_path=rules_file)
    with pytest.raises(ValueError):
        argue(rules, "account.max_concurrent_positions", "2.5", rules_path=rules_file)


def test_an_unknown_key_is_refused_rather_than_edited(rules, rules_file):
    """An agent that accepted an arbitrary dotted path could be asked to edit
    any line in the file, including one nobody wrote a consequence for."""
    with pytest.raises(UnknownLimit):
        argue(rules, "account.something_invented", "1", rules_path=rules_file)


def test_a_list_has_no_number_to_argue_about(rules, rules_file):
    """`allowed_symbols` is a permission and is worth explaining — the reasoning
    is on the Settings page and in the briefing — but coercing a list to a
    number would produce an argument about a figure nobody meant."""
    fact = limits_for(rules)["instruments.us_equity.allowed_symbols"]
    assert fact.unit is Unit.LIST
    assert not fact.requestable
    assert "no history" in fact.cost

    with pytest.raises(ValueError):
        argue(rules, "instruments.us_equity.allowed_symbols", "1", rules_path=rules_file)


# ------------------------------------------------- the argument, end to end


def test_the_operator_argues_a_loosening_through_and_the_objection_survives(
    rules, rules_file, store
):
    """The whole feature in one test, with no model anywhere in it.

    The operator asks to double the per-trade cap. The agent states the
    arithmetic and records NOTHING. The operator insists. The change request is
    recorded with the objection preserved beside their reason — because an
    argument the operator won is a fact worth keeping, and so is one they lost.
    """
    before = rules_file.read_text()

    # 1. The ask. Challenged, costed, and nothing written.
    argument = argue(
        rules,
        "account.max_risk_per_trade_pct",
        "2.0",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )
    assert argument.requires_confirmation
    assert "2.00x the loss" in " ".join(argument.consequence)
    assert store.recent() == []

    # 2. The operator insists, having read it.
    record = build_request(
        argument,
        reason="I am comfortable with a larger single loss on this account.",
        transcript=[
            {"role": "operator", "text": "raise the per-trade cap to 2%"},
            {"role": "armorer", "text": "two per cent is two thousand a trade."},
            {"role": "operator", "text": "understood. do it."},
        ],
        rules_path=rules_file,
    )
    store.record(record)

    # 3. What was kept.
    kept = store.get(record.id)
    assert kept is not None
    assert kept.key == "account.max_risk_per_trade_pct"
    assert (kept.old_value, kept.new_value) == ("1.0", "2.0")
    assert kept.stance == "loosening"
    assert kept.status == "pending"
    assert "comfortable with a larger single loss" in kept.reason
    # The objection is the part that must not be lost when the operator wins.
    assert "bigger position at the same stop" in kept.objection
    assert "2.00x the loss" in " ".join(kept.consequence)
    assert len(kept.transcript) == 3

    # 4. And the file has not moved. Recording is not applying.
    assert rules_file.read_text() == before
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 1.0


def test_a_tightening_goes_through_with_less_friction(rules, rules_file, store):
    """The other half of the asymmetry. One step, no confirmation, no objection,
    and the same record — because a limit getting tighter still changes what the
    gate does and still wants a commit somebody can read."""
    argument = argue(
        rules,
        "account.max_risk_per_trade_pct",
        "0.5",
        equity_usd=EQUITY,
        rules_path=rules_file,
    )

    assert not argument.requires_confirmation
    assert argument.objection == ""

    record = store.record(
        build_request(argument, reason="Smaller size while I watch it.", rules_path=rules_file)
    )
    kept = store.get(record.id)

    assert kept is not None
    assert kept.stance == "tightening"
    assert kept.objection == ""
    assert "$500.00 instead of $1,000.00" in " ".join(kept.consequence)


def test_the_commit_message_carries_both_sides_of_the_argument(rules, rules_file, store):
    """A commit that recorded only the winning side would make the argument look
    like it never happened. `CLAUDE.md`: limits change in their own commit, with
    a reason."""
    argument = argue(
        rules, "account.max_total_risk_pct", "4.0", equity_usd=EQUITY, rules_path=rules_file
    )
    record = store.record(
        build_request(argument, reason="Running two strategies now.", rules_path=rules_file)
    )
    message = commit_message(record)

    assert message.startswith("config: loosen account.max_total_risk_pct from 2.0 to 4.0")
    assert "Running two strategies now." in message
    assert "The Armorer objected" in message
    assert f"settings request {record.id}" in message


# ------------------------------------------------------------------ the diff


def test_the_diff_moves_one_line_and_keeps_every_comment(rules, rules_file):
    """`config/rules.yaml` is mostly reasoning; the numbers are the small part.

    Re-serialising the parsed model would round-trip every comment away, which
    is why the diff is produced against the text on disk.
    """
    text = rules_file.read_text()
    fact = limits_for(rules)["account.max_risk_per_trade_pct"]
    diff = render_diff(text, fact, 2.0, name="rules.yaml")

    removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    added = [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]

    assert removed == ["-  max_risk_per_trade_pct: 1.0"]
    assert added == ["+  max_risk_per_trade_pct: 2.0"]
    # The comment above the line is context in the diff, so it survives.
    assert "A wider stop means a SMALLER" in diff


def test_the_new_value_is_written_in_the_notation_the_file_uses(rules, rules_file):
    """`1.0` rather than `1`. A diff that also changed the notation would be a
    second change riding along with the first."""
    text = rules_file.read_text()
    facts = limits_for(rules)

    assert "+  max_position_pct: 40.0" in render_diff(
        text, facts["account.max_position_pct"], 40.0, name="rules.yaml"
    )
    assert "+  max_concurrent_positions: 2" in render_diff(
        text, facts["account.max_concurrent_positions"], 2, name="rules.yaml"
    )


# ----------------------------------------------------------------- applying


def test_applying_edits_exactly_one_line_and_leaves_the_rest_byte_identical(
    rules, rules_file, store
):
    before = rules_file.read_text().splitlines()
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    result = apply_request(store, record.id, rules_path=rules_file)
    after = rules_file.read_text().splitlines()

    assert result.ok, result.detail
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    assert after[changed[0]].strip() == "max_position_pct: 40.0"
    assert Rules.load(rules_file).account.max_position_pct == 40.0


def test_an_applied_request_is_marked_and_cannot_be_applied_twice(
    rules, rules_file, store
):
    """A decided request is history rather than a queue entry. Re-running the
    command must not silently re-edit a file that has moved on."""
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    assert apply_request(store, record.id, rules_path=rules_file).ok

    again = apply_request(store, record.id, rules_path=rules_file)
    assert not again.ok
    assert "not pending" in again.detail

    kept = store.get(record.id)
    assert kept is not None and kept.status == "applied"
    assert kept.applied_at is not None


def test_a_file_that_moved_since_the_argument_refuses_to_be_edited(
    rules, rules_file, store
):
    """An argument made against one version of the file is not an argument about
    a different one. The consequence stated at the time was computed against
    what the file said then."""
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    rules_file.write_text(rules_file.read_text() + "\n# somebody edited this by hand\n")

    result = apply_request(store, record.id, rules_path=rules_file)

    assert not result.ok
    assert "has changed since request" in result.detail
    assert store.get(record.id).status == "pending"


def test_an_edit_that_would_not_load_is_refused_and_writes_nothing(
    rules, rules_file, store
):
    """`max_total_risk_pct` below `max_risk_per_trade_pct` means no trade could
    ever be opened, and `AccountRules` says so. The apply step re-validates
    through `Rules.load` — the same loader the bot starts on — so a config that
    passes here is one the bot can start on."""
    before = rules_file.read_text()
    argument = argue(rules, "account.max_total_risk_pct", "0.5", rules_path=rules_file)
    record = store.record(
        build_request(argument, reason="Much tighter.", rules_path=rules_file)
    )

    result = apply_request(store, record.id, rules_path=rules_file)

    assert not result.ok
    assert "does not load" in result.detail
    assert rules_file.read_text() == before
    # And no temporary file was left lying beside the real one.
    assert not list(rules_file.parent.glob("*.tmp"))


def test_a_dry_run_proves_it_loads_and_writes_nothing(rules, rules_file, store):
    before = rules_file.read_text()
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    result = apply_request(store, record.id, rules_path=rules_file, dry_run=True)

    assert result.ok
    assert "Nothing was written" in result.detail
    assert rules_file.read_text() == before
    assert store.get(record.id).status == "pending"


def test_a_request_for_a_missing_id_is_named_rather_than_raising(store):
    result = apply_request(store, 999)

    assert not result.ok
    assert "No settings request 999" in result.detail


def test_the_digest_is_what_makes_the_staleness_check_mean_anything(rules_file):
    text = rules_file.read_text()

    assert digest(text) == digest(text)
    assert digest(text) != digest(text + "\n")


# ------------------------------------------------------------- the revert

def test_a_revert_puts_back_the_exact_text_that_was_on_the_line(
    rules, rules_file, store
):
    """`90000` and `90000.0` are one limit and two different diffs.

    The previous value is recorded as the exact characters on the line, which is
    what makes the undo need no guesswork — and what stops a revert being a
    second change riding along with the first.
    """
    before = rules_file.read_text()
    argument = argue(rules, "account.min_equity_floor_usd", "80000", rules_path=rules_file)
    record = store.record(
        build_request(argument, reason="More room to breathe.", rules_path=rules_file)
    )

    assert record.old_value == "90000"  # not "90000.0"
    assert apply_request(store, record.id, rules_path=rules_file).ok
    assert Rules.load(rules_file).account.min_equity_floor_usd == 80000.0

    result = revert_request(store, record.id, rules_path=rules_file)

    assert result.ok, result.detail
    # Byte-identical, comments and notation included.
    assert rules_file.read_text() == before


def test_reverting_does_not_erase_that_it_was_applied(rules, rules_file, store):
    """The row has to read as what actually happened: the limit moved at this
    moment, and was put back at that one. Clearing the apply stamp would leave a
    record saying a change was never made, of a change that was made."""
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    apply_request(store, record.id, rules_path=rules_file, actor="root")
    revert_request(store, record.id, rules_path=rules_file, actor="mudhorn via sudo")

    kept = store.get(record.id)
    assert kept is not None
    assert kept.status == "reverted"
    assert kept.applied_at is not None and kept.applied_by == "root"
    assert kept.reverted_at is not None and kept.reverted_by == "mudhorn via sudo"


def test_a_revert_can_only_restore_a_value_the_file_already_carried(
    rules, rules_file, store
):
    """So it is not a route around the confirmation gate.

    Tighten on the first ask, then undo, and the file is back where it started —
    it cannot reach a number the file never held. That is why reverting a
    tightening is not itself gated as a loosening: gating it would mean arguing
    with an operator undoing something, which is the wall this module exists
    instead of.
    """
    started_at = Rules.load(rules_file).account.max_risk_per_trade_pct
    tighten = argue(rules, "account.max_risk_per_trade_pct", "0.5", rules_path=rules_file)
    assert tighten.stance is Stance.TIGHTENING
    record = store.record(build_request(tighten, reason="Careful.", rules_path=rules_file))

    apply_request(store, record.id, rules_path=rules_file)
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 0.5

    revert_request(store, record.id, rules_path=rules_file)

    assert Rules.load(rules_file).account.max_risk_per_trade_pct == started_at


def test_a_line_moved_since_the_change_refuses_to_be_reverted(rules, rules_file, store):
    """Putting the old value back would overwrite a change nobody in this
    conversation discussed."""
    first = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(first, reason="Tighter.", rules_path=rules_file))
    apply_request(store, record.id, rules_path=rules_file)

    # Somebody else moves the same line afterwards.
    moved = argue(
        Rules.load(rules_file), "account.max_position_pct", "45.0", rules_path=rules_file
    )
    second = store.record(build_request(moved, reason="Bit more.", rules_path=rules_file))
    apply_request(store, second.id, rules_path=rules_file)

    result = revert_request(store, record.id, rules_path=rules_file)

    assert not result.ok
    assert "Something moved it since" in result.detail
    assert Rules.load(rules_file).account.max_position_pct == 45.0


def test_a_request_that_never_moved_the_file_cannot_be_reverted(rules, rules_file, store):
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    result = revert_request(store, record.id, rules_path=rules_file)

    assert not result.ok
    assert "nothing to put back" in result.detail


def test_a_revert_cannot_be_run_twice(rules, rules_file, store):
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))
    apply_request(store, record.id, rules_path=rules_file)
    assert revert_request(store, record.id, rules_path=rules_file).ok

    again = revert_request(store, record.id, rules_path=rules_file)

    assert not again.ok
    assert "already reverted" in again.detail


def test_a_revert_that_would_not_load_is_refused_and_writes_nothing(
    rules, rules_file, store
):
    """The undo goes through the same staged validation as the change. A file
    that will not load is refused whichever direction it was moving in."""
    # Tighten the total below what the per-trade cap needs, in two steps, so the
    # revert of the FIRST would restore an incoherent pair.
    step_one = argue(rules, "account.max_risk_per_trade_pct", "0.4", rules_path=rules_file)
    first = store.record(build_request(step_one, reason="Smaller.", rules_path=rules_file))
    apply_request(store, first.id, rules_path=rules_file)

    reloaded = Rules.load(rules_file)
    step_two = argue(reloaded, "account.max_total_risk_pct", "0.5", rules_path=rules_file)
    second = store.record(build_request(step_two, reason="Smaller too.", rules_path=rules_file))
    apply_request(store, second.id, rules_path=rules_file)

    before = rules_file.read_text()
    result = revert_request(store, first.id, rules_path=rules_file)

    assert not result.ok
    assert "does not load" in result.detail
    assert rules_file.read_text() == before
    assert not list(rules_file.parent.glob("*.tmp"))


# ------------------------------------------------- reaching the privilege


def test_the_id_travels_on_stdin_and_the_wrapper_takes_no_arguments(
    rules, rules_file, store, tmp_path
):
    """The whole reason the sudoers rule names a wrapper rather than the binary.

    Anything in argv is something a future release could interpret as a flag.
    The id goes down stdin, where it cannot be read as one whatever it contains.
    """
    fake_sudo = tmp_path / "sudo"
    fake_sudo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sudo.chmod(0o755)
    applier, calls = _stub_wrapper(store, rules_file, tmp_path)
    applier = WrapperApplier(
        wrapper=applier.wrapper, sudo=str(fake_sudo), runner=applier.runner
    )
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    assert applier.apply(record.id).ok

    argv, stdin = calls[0]
    assert argv == [str(fake_sudo), "-n", "-u", "root", "--", str(applier.wrapper)]
    assert stdin == f"apply {record.id}\n"
    # Nothing about the change is in argv. Not the key, not the value, not the
    # path to the rules file.
    assert not any("max_position_pct" in part for part in argv)


def test_a_missing_wrapper_is_reported_rather_than_guessed_at(tmp_path):
    applier = WrapperApplier(wrapper=tmp_path / "not-installed.sh", sudo="")

    state = applier.available

    assert not state.ok
    assert "not installed" in state.detail
    assert not applier.apply(1).ok


def test_a_wrapper_that_fails_reports_what_it_said(tmp_path):
    """The operator gets the wrapper's own words. A generic "could not apply"
    would hide which of several different problems it was."""
    script = tmp_path / "apply-settings.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def runner(argv, stdin, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")

    result = WrapperApplier(wrapper=script, sudo="", runner=runner).apply(3)

    assert not result.ok
    assert "a password is required" in result.detail


def test_a_wrapper_that_hangs_says_the_file_state_is_unknown(tmp_path):
    """A timeout is the one failure where "nothing was written" cannot be
    claimed, so it is not claimed."""
    script = tmp_path / "apply-settings.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def runner(argv, stdin, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    result = WrapperApplier(wrapper=script, sudo="", runner=runner).apply(3)

    assert not result.ok
    assert "Nothing can be said about whether the file moved" in result.detail


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_wrapper_refuses_anything_that_is_not_an_id(tmp_path):
    """The script is what the sudoers rule points at, so it validates its own
    input rather than trusting the caller.

    Run directly, never through sudo, against a fake APP_DIR whose
    `electrum-bot` only echoes its arguments. That is enough to prove both
    halves: junk is refused before anything runs, and a well-formed id reaches
    exactly one fixed command.
    """
    app = tmp_path / "app"
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / "config").mkdir()
    (app / "data").mkdir()
    shutil.copy(DEFAULT_RULES_PATH, app / "config" / "rules.yaml")
    stub = app / ".venv" / "bin" / "electrum-bot"
    stub.write_text('#!/bin/sh\necho "CALLED $*"\n', encoding="utf-8")
    stub.chmod(0o755)

    def run(text: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(WRAPPER_SOURCE)],
            input=text,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "APP_DIR": str(app), "APP_USER": "nobody"},
            check=False,
        )

    for junk in ("", "; rm -rf /", "apply --rules /etc/shadow", "revert 4x", "apply 4; id"):
        refused = run(junk + "\n")
        assert refused.returncode == 2, junk
        assert "Nothing was done" in refused.stderr
        assert "CALLED" not in refused.stdout

    applied = run("apply 4\n")
    assert applied.returncode == 0
    assert applied.stdout.startswith("CALLED settings-apply 4 --rules ")

    # A bare number is an apply, which is what "reads a request id on stdin"
    # means at its narrowest.
    assert run("4\n").stdout.startswith("CALLED settings-apply 4 ")
    assert run("revert 4\n").stdout.startswith("CALLED settings-revert 4 ")


# ------------------------------------------- argue, record and apply, in one


def test_a_tightening_is_applied_on_the_first_ask(rules, rules_file, store, tmp_path):
    """Somebody choosing to lose less is not interrogated for it, and now it
    also takes effect in the same breath."""
    applier, _ = _stub_wrapper(store, rules_file, tmp_path)

    outcome = decide(
        store,
        rules,
        "account.max_risk_per_trade_pct",
        "0.5",
        reason="Smaller size while I watch it.",
        equity_usd=EQUITY,
        rules_path=rules_file,
        applier=applier,
    )

    assert outcome.argument.stance is Stance.TIGHTENING
    assert outcome.changed
    assert outcome.request is not None
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 0.5
    assert store.get(outcome.request.id).status == "applied"
    assert "is now 0.5, was 1.0" in outcome.summary


def test_a_loosening_is_not_applied_until_the_operator_confirms(
    rules, rules_file, store, tmp_path
):
    """The whole feature, and the half that had to survive the reversal.

    The first ask states the arithmetic and writes NOTHING — not the file, not
    a row. The second, confirmed, applies it, and the objection is on the record
    even though the Armorer lost the argument.
    """
    applier, _ = _stub_wrapper(store, rules_file, tmp_path)
    before = rules_file.read_text()
    ask = dict(
        reason="I am comfortable with a larger single loss.",
        equity_usd=EQUITY,
        rules_path=rules_file,
        applier=applier,
    )

    challenged = decide(
        store, rules, "account.max_risk_per_trade_pct", "2.0", **ask
    )

    assert challenged.argument.requires_confirmation
    assert "2.00x the loss" in " ".join(challenged.argument.consequence)
    assert challenged.request is None
    assert challenged.applied is None
    assert store.recent() == []
    assert rules_file.read_text() == before

    agreed = decide(
        store, rules, "account.max_risk_per_trade_pct", "2.0", confirm=True, **ask
    )

    assert agreed.changed
    assert agreed.request is not None
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 2.0
    kept = store.get(agreed.request.id)
    assert kept is not None and kept.status == "applied"
    assert "bigger position at the same stop" in kept.objection
    assert "comfortable with a larger single loss" in kept.reason


def test_a_value_that_would_not_load_is_refused_with_the_file_untouched(
    rules, rules_file, store, tmp_path
):
    """`max_total_risk_pct` below the per-trade cap means no trade could ever
    open. The staged copy is where that is found, so the file the loop reads
    never holds it."""
    applier, _ = _stub_wrapper(store, rules_file, tmp_path)
    before = rules_file.read_text()

    outcome = decide(
        store,
        rules,
        "account.max_total_risk_pct",
        "0.5",
        reason="Much tighter.",
        rules_path=rules_file,
        applier=applier,
    )

    assert outcome.recorded, "the argument still happened and is still on file"
    assert outcome.request is not None and outcome.applied is not None
    assert not outcome.changed
    assert "does not load" in outcome.applied.detail
    assert rules_file.read_text() == before
    assert store.get(outcome.request.id).status == "pending"
    assert not list(rules_file.parent.glob("*.tmp"))


def test_without_a_privileged_route_the_request_survives_and_says_so(
    rules, rules_file, store, tmp_path
):
    """The fallback is the previous behaviour, reported plainly — never a
    silence. Same rule as the Dreaming page's isolation banner: report the
    weaker fact rather than imply the stronger one."""
    before = rules_file.read_text()

    outcome = decide(
        store,
        rules,
        "account.max_position_pct",
        "40.0",
        reason="Tighter.",
        rules_path=rules_file,
        applier=WrapperApplier(wrapper=tmp_path / "absent.sh", sudo=""),
    )

    assert outcome.recorded
    assert outcome.request is not None and outcome.applied is not None
    assert not outcome.changed
    assert "not installed" in outcome.applied.detail
    assert rules_file.read_text() == before
    assert store.get(outcome.request.id).status == "pending"
    assert "NOT applied" in outcome.summary
    assert "settings-apply" in outcome.summary


def test_an_unrecordable_limit_never_reaches_the_privileged_route(
    rules, rules_file, store, tmp_path
):
    """A list is explained and cannot become a diff, so nothing is asked of
    root about it."""
    applier, calls = _stub_wrapper(store, rules_file, tmp_path)

    outcome = decide(
        store,
        rules,
        "instruments.us_equity.max_class_total_risk_pct",
        "1.5",
        confirm=True,
        rules_path=rules_file,
        applier=applier,
    )

    assert not outcome.argument.can_record
    assert outcome.request is None
    assert calls == []
    assert store.recent() == []


def test_the_direct_applier_is_the_shell_route_and_says_which(
    rules, rules_file, store
):
    """A caller that already holds the permission does not need sudo, and the
    result names which route wrote the file."""
    applier = DirectApplier(store, rules_path=rules_file)
    argument = argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file)
    record = store.record(build_request(argument, reason="Tighter.", rules_path=rules_file))

    result = applier.apply(record.id)

    assert result.ok and result.via == "in process"
    assert Rules.load(rules_file).account.max_position_pct == 40.0
    assert applier.revert(record.id).ok
    assert Rules.load(rules_file).account.max_position_pct == 50.0


# ------------------------------------------------------------- the migration


def test_a_store_that_predates_the_revert_columns_keeps_its_rows(tmp_path):
    """The lesson `journal.SCHEMA` cost a live order, in a second store.

    `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists, and
    every other test here builds its store fresh in a `tmp_path` — so the suite
    is structurally blind to a schema change that never reaches the database on
    the droplet. This test builds the OLD shape deliberately, which is the only
    way to exercise a migration at all, and asserts the existing row survives.
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
          old_value TEXT NOT NULL, new_value TEXT NOT NULL, stance TEXT NOT NULL,
          reason TEXT NOT NULL, objection TEXT NOT NULL, consequence TEXT NOT NULL,
          transcript TEXT NOT NULL, rules_digest TEXT NOT NULL,
          rules_path TEXT NOT NULL, diff TEXT NOT NULL, created_at TEXT NOT NULL,
          status TEXT NOT NULL, applied_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO requests VALUES (1,'account.max_position_pct','50.0','40.0',"
        "'tightening','because','','[]','[]','','rules.yaml','','2026-01-01T00:00:00+00:00',"
        "'applied','2026-01-02T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = SettingsRequestStore(path)
    kept = store.get(1)

    assert kept is not None
    assert kept.key == "account.max_position_pct"
    assert kept.status == "applied"
    assert kept.applied_at is not None
    # The new columns read as empty rather than raising, which is the honest
    # answer for a change made before anything recorded a route.
    assert kept.applied_by == ""
    assert kept.reverted_at is None and kept.reverted_by == ""

    # And the migration is idempotent: opening it again changes nothing.
    reopened = SettingsRequestStore(path).get(1)
    assert reopened is not None and reopened.status == "applied"


# -------------------------------------------------------- what the model gets


def test_the_briefing_hands_over_figures_rather_than_asking_for_them(rules):
    """Same rule as `indicators.py`, at a different surface. A model asked to
    work out what doubling a cap costs will produce a number, state it
    confidently, and be believed."""
    briefing = render_briefing(rules, equity_usd=EQUITY)

    assert "account.max_risk_per_trade_pct = 1.0" in briefing
    assert "Last equity reading: $100,000.00." in briefing
    assert "Quote them exactly" in briefing
    assert "Do not compute a consequence yourself" in briefing


def test_the_briefing_describes_the_reach_it_actually_has(rules, tmp_path):
    """What the agent can DO is probed, never assumed.

    It used to be told flatly that it could not change anything, which was true
    then and is a lie on a box with the wrapper installed. Telling it the other
    flat thing would be a lie on a box without one. Both halves are asserted
    here because getting either wrong puts a confident false claim in front of
    an operator deciding what to risk.
    """
    absent = render_briefing(rules, applier=WrapperApplier(wrapper=tmp_path / "nope.sh"))

    assert "YOU CANNOT CHANGE ANY OF THESE FROM HERE" in absent
    assert "root-owned" in absent
    assert "settings-apply" in absent
    assert "Say RECORDED, never changed" in absent

    present = render_briefing(rules, applier=_always_available())

    assert "YOU CAN CHANGE THESE" in present
    # The asymmetry reaches the model as a fact about the code, not as advice.
    assert "applies NOTHING until the operator confirms" in present
    assert "Say APPLIED only when the tool tells you it applied" in present
    assert "settings-revert" in present


def test_the_briefing_states_a_missing_equity_reading(rules):
    assert "Dollar figures are unknown, not estimated." in render_briefing(rules)


# ------------------------------------------------------------- the surface


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bot.audit import AuditLog  # noqa: E402
from bot.config import Env  # noqa: E402
from bot.dreaming import DreamStore  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.web import live  # noqa: E402
from bot.web.app import build_app  # noqa: E402

TOKEN = "forge-token"


def _client(
    tmp_path, rules_file, store, *, token: str = TOKEN, read: bool = True, applier=None
):
    """An app with every store pointed at `tmp_path`.

    All of them, deliberately. `tests/conftest.py` fails the suite if anything
    lands in the repository's own `data/` or `audit/`, and a new store with a
    production default is exactly how that regressed the last time.

    **The applier is injected for the same class of reason and a sharper
    version of it.** Left to its default the route reaches for
    `/opt/mudhorn/deploy/apply-settings.sh`, which is absent here — so every
    test would pass because of a path that does not exist rather than because of
    the code, and the suite would start editing a live risk limit the day
    somebody ran it on the droplet. `None` means "no privileged route", which is
    a real deployment and is asserted as one below.
    """
    env = Env(_env_file=None)  # type: ignore[call-arg]
    env.dashboard_chat_token = token
    journal = Journal(tmp_path / "journal.db")
    poller = live.build_poller(
        journal=journal,
        env=Env(_env_file=None),  # type: ignore[call-arg]
        force_mock=True,
    )
    if read:
        # The forge quotes equity, and equity comes from the poller. Without a
        # reading it says so rather than assuming one — which is its own test.
        poller.poll_once()
    app = build_app(
        journal=journal,
        rules=Rules.load(rules_file),
        env=env,
        audit_log=AuditLog(tmp_path / "audit"),
        dreams=DreamStore(tmp_path / "dreams.db"),
        poller=poller,
        settings_requests=store,
        settings_applier=applier or WrapperApplier(wrapper=tmp_path / "absent.sh", sudo=""),
        rules_path=rules_file,
        force_mock=True,
    )
    return TestClient(app)


def test_the_forge_adds_exactly_the_controls_that_were_decided_on(
    tmp_path, rules_file, store
):
    """The read-only rule, widened a third time and deliberately.

    `tests/test_web.py` asserts the Settings page carries no `<form`, `<input`,
    `<select`, `<button` or `contenteditable` — and it still does with the forge
    OFF, which is the default. With it on, four controls appear and each is
    named here:

    - a `<select>` choosing WHICH limit, from the agent's own table
    - an `<input>` for the proposed value
    - a `<textarea>` for the operator's reason, which is recorded and reaches
      the commit message
    - `<button>`s to ask and, separately, to confirm a loosening

    **They are permitted because none of them can change a limit.** There is no
    `<form>` at all — nothing here submits to a page — and the only endpoint the
    page posts to is `/settings/request`, which records a change request.
    `config/` is root-owned on the box so this process cannot write
    `config/rules.yaml`, and applying a request is a separate privileged step.
    """
    body = _client(tmp_path, rules_file, store).get("/settings").text

    for control in ("<select", "<input", "<textarea", "<button"):
        assert control in body, f"the forge is missing its {control}"
    # No form element anywhere: a form posts to a URL and would work with the
    # script blocked, which is precisely the affordance this must not have.
    assert "<form" not in body.lower()
    assert "contenteditable" not in body.lower()

    # And the only endpoint the forge talks to is the one that records a
    # request. `/session` and `/live` belong to the shell and the stream and are
    # both GETs; `/chat` appears only when Hermes is installed, which it is not
    # here. Anything else in this set is a write target nobody decided on.
    posted = set(re.findall(r"fetch\('([^']+)'", body))
    assert "/settings/request" in posted
    assert posted <= {"/settings/request", "/chat", "/session", "/live"}, posted


def test_the_page_says_how_the_change_actually_reaches_the_file(
    tmp_path, rules_file, store
):
    """The controls change a limit now, so the page has to describe the
    arrangement rather than deny it — and the denial is exactly the prose that
    stops being checked once it is no longer true."""
    body = _client(tmp_path, rules_file, store).get("/settings").text

    assert "The forge" in body
    # Still true, and still the reason any of this is safe.
    assert "owned by root on the box" in body
    assert "cannot write" in body
    assert "with its own hands" in body
    # And the fallback is named where an operator on a laptop will meet it.
    assert "recorded and not applied" in body.lower()
    assert "settings-apply" in body
    assert "settings-revert" in body


def test_the_forge_is_off_without_a_token_and_says_why(tmp_path, rules_file, store):
    """Fail closed. A deploy must not switch on the ability to drive an agent
    and record a change to a risk limit."""
    client = _client(tmp_path, rules_file, store, token="")
    body = client.get("/settings").text

    assert "The forge is off" in body
    assert "DASHBOARD_CHAT_TOKEN" in body
    # `<textarea` is deliberately not in this list: the stylesheet mentions the
    # chat textarea in a CSS comment, so asserting on it would fail over prose.
    # The forge's own controls carry `fg-` ids, which is the precise question.
    for control in ("<select", "<input", "<button", "<form"):
        assert control not in body.lower()
    assert 'id="fg-' not in body

    assert client.post("/settings/request", json={"key": "x", "value": "1"}).status_code == 404


def test_the_route_refuses_a_wrong_token(tmp_path, rules_file, store):
    """Viewing an account and recording a change to a risk limit are different
    privileges, and one secret must not grant both."""
    client = _client(tmp_path, rules_file, store)

    r = client.post(
        "/settings/request",
        json={"token": "wrong", "key": "account.max_position_pct", "value": "40"},
    )

    assert r.status_code == 403
    assert store.recent() == []


def test_the_route_challenges_a_loosening_before_it_will_record_it(
    tmp_path, rules_file, store
):
    """The argument end to end over HTTP, with no model in it.

    First ask: the consequence, in figures, and nothing written. Second ask,
    confirmed: recorded, with the objection preserved and the diff and commit
    message ready to apply.
    """
    client = _client(tmp_path, rules_file, store)
    ask = {
        "token": TOKEN,
        "key": "account.max_risk_per_trade_pct",
        "value": "2.0",
        "reason": "I want larger positions.",
    }

    challenged = client.post("/settings/request", json=ask).json()

    assert challenged["stance"] == "loosening"
    assert challenged["requires_confirmation"] is True
    assert challenged["recorded"] is None
    assert challenged["applied"] is None
    assert store.recent() == []
    said = " ".join(challenged["consequence"])
    assert "2.00x the loss" in said
    assert "$2,000.00 instead of $1,000.00" in said
    assert "1 full-size trade fits" in said

    # Everything the confirmation window renders is on the UNCONFIRMED leg. It
    # has no arithmetic and no fetch in it, so a payload missing these would ask
    # the operator to agree to a number with nothing to compare it against.
    assert challenged["label"] == "Risk per trade"
    assert challenged["unit"] == "per cent of equity"
    assert (challenged["old_value"], challenged["new_value"]) == ("1.0", "2.0")
    assert "-  max_risk_per_trade_pct: 1.0" in challenged["diff"]
    assert "+  max_risk_per_trade_pct: 2.0" in challenged["diff"]
    assert "bigger position at the same stop" in challenged["objection"]

    confirmed = client.post("/settings/request", json={**ask, "confirm": True}).json()
    recorded = confirmed["recorded"]

    assert recorded["id"] == 1
    assert "-  max_risk_per_trade_pct: 1.0" in recorded["diff"]
    assert "+  max_risk_per_trade_pct: 2.0" in recorded["diff"]
    assert "settings-apply 1" in recorded["apply_command"]
    assert "settings-revert 1" in recorded["revert_command"]
    assert "The Armorer objected" in recorded["commit_message"]

    kept = store.get(1)
    assert kept is not None
    assert "bigger position at the same stop" in kept.objection


def test_the_route_records_a_tightening_on_the_first_ask(tmp_path, rules_file, store):
    client = _client(tmp_path, rules_file, store)

    answer = client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "account.max_risk_per_trade_pct",
            "value": "0.5",
            "reason": "Smaller while I watch it.",
        },
    ).json()

    assert answer["stance"] == "tightening"
    assert answer["requires_confirmation"] is False
    assert answer["recorded"]["id"] == 1
    assert store.get(1).objection == ""


def test_with_no_privileged_route_the_surface_records_and_the_file_stands(
    tmp_path, rules_file, store
):
    """The fallback deployment, asserted rather than assumed.

    A laptop, or a box where the sudoers rule was never installed. Every request
    is argued and recorded; the file is byte-identical afterwards; and the
    answer says so in the operator's own words rather than reading as a change
    that landed.
    """
    before = rules_file.read_text()
    client = _client(tmp_path, rules_file, store)

    answers = [
        client.post(
            "/settings/request",
            json={
                "token": TOKEN,
                "key": "account.max_risk_per_trade_pct",
                "value": value,
                "reason": "testing",
                "confirm": True,
            },
        ).json()
        for value in ("2.0", "0.5", "0.9")
    ]

    assert rules_file.read_text() == before
    assert len(store.recent()) == 3
    for answer in answers:
        assert answer["recorded"]["id"]
        assert answer["applied"]["ok"] is False
        assert "not installed" in answer["applied"]["detail"]
        assert "NOT applied" in answer["summary"]
    assert all(req.status == "pending" for req in store.recent())


def test_the_surface_applies_the_change_when_the_route_is_there(
    tmp_path, rules_file, store
):
    """The reversal, end to end over HTTP and with no model in it.

    A tightening lands on the first ask. A loosening is refused until the
    operator confirms having read the cost, then lands — with the objection
    still on the record, because an argument the operator won is a fact worth
    keeping and so is one they lost.
    """
    applier, calls = _stub_wrapper(store, rules_file, tmp_path)
    client = _client(tmp_path, rules_file, store, applier=applier)

    tightened = client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "account.max_position_pct",
            "value": "40.0",
            "reason": "Smaller concentration backstop.",
        },
    ).json()

    assert tightened["applied"]["ok"] is True
    assert Rules.load(rules_file).account.max_position_pct == 40.0
    assert "is now 40.0, was 50.0" in tightened["summary"]

    ask = {
        "token": TOKEN,
        "key": "account.max_risk_per_trade_pct",
        "value": "2.0",
        "reason": "I am comfortable with a larger single loss.",
    }
    challenged = client.post("/settings/request", json=ask).json()

    assert challenged["applied"] is None
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 1.0

    agreed = client.post("/settings/request", json={**ask, "confirm": True}).json()

    assert agreed["applied"]["ok"] is True
    assert Rules.load(rules_file).account.max_risk_per_trade_pct == 2.0
    kept = store.get(agreed["recorded"]["id"])
    assert kept.status == "applied" and kept.applied_at is not None
    assert "bigger position at the same stop" in kept.objection

    # Two changes, two trips through the wrapper, and the id on stdin both
    # times. Nothing about either change was ever in argv.
    assert [stdin for _, stdin in calls] == ["apply 1\n", "apply 2\n"]


def test_the_transcript_is_kept_with_the_request(tmp_path, rules_file, store):
    """An argument the operator won is a fact worth keeping, and so is one they
    lost. Six months on the useful question is not what the limit is — the file
    says that — but what was said before it moved."""
    client = _client(tmp_path, rules_file, store)

    client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "frequency.max_trades_per_day",
            "value": "9",
            "reason": "More opportunities lately.",
            "confirm": True,
            "transcript": [
                {"role": "operator", "text": "nine a day"},
                {"role": "armorer", "text": "fees dominated P&L in Alpha Arena."},
            ],
        },
    )

    kept = store.get(1)
    assert kept is not None
    assert [t["role"] for t in kept.transcript] == ["operator", "armorer"]
    assert "Alpha Arena" in kept.transcript[1]["text"]


def test_a_recorded_request_is_rendered_with_its_objection(tmp_path, rules_file, store):
    """The page must show the argument, not only the outcome. A request list
    that showed the winning side alone would make the objection look like it
    never happened."""
    client = _client(tmp_path, rules_file, store)
    client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "account.max_risk_per_trade_pct",
            "value": "2.0",
            "reason": "I want larger positions.",
            "confirm": True,
        },
    )

    body = client.get("/settings").text

    assert "The Armorer objected" in body
    assert "I want larger positions." in body
    assert "settings-apply 1" in body


def test_the_forge_reports_an_unread_equity_rather_than_assuming_one(
    tmp_path, rules_file, store
):
    """A cold start has no broker reading. `$100,000` on that page would be a
    plausible wrong figure in front of somebody deciding what to risk."""
    client = _client(tmp_path, rules_file, store, read=False)

    said = " ".join(
        client.post(
            "/settings/request",
            json={
                "token": TOKEN,
                "key": "account.max_risk_per_trade_pct",
                "value": "2.0",
            },
        ).json()["consequence"]
    )

    assert "unknown rather than estimated" in said
    assert "$" not in said


def test_a_limit_this_path_cannot_edit_explains_itself(tmp_path, rules_file, store):
    """An operator told "no" with no explanation learns nothing. `us_equity` has
    no class-total line at all, so adding one is a new setting rather than a
    change to one — and that belongs in a commit where the new line can carry
    its own comment."""
    client = _client(tmp_path, rules_file, store)

    answer = client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "instruments.us_equity.max_class_total_risk_pct",
            "value": "1.5",
            "confirm": True,
        },
    ).json()

    assert answer["can_record"] is False
    assert "has no line in rules.yaml" in answer["blocked_reason"]
    assert answer["consequence"], "it should still explain what the change would do"
    assert store.recent() == []


def test_an_unknown_key_from_the_browser_is_an_error_and_not_a_500(
    tmp_path, rules_file, store
):
    client = _client(tmp_path, rules_file, store)

    answer = client.post(
        "/settings/request",
        json={"token": TOKEN, "key": "../../etc/passwd", "value": "1"},
    ).json()

    assert answer["ok"] is False
    assert "not a limit this agent knows about" in answer["error"]


def test_the_armorer_is_selectable_on_the_chat_route(tmp_path, rules_file, store, monkeypatch):
    """The soul reaches the model, and so does the limit table — as figures the
    agent must not derive. Only the Armorer gets the briefing: handing the
    account agent a table of caps it has no reason to argue about would cost
    tokens on every question."""
    from bot.web import chat as chat_mod

    seen: list[str] = []

    def _ask(self, message, history=None, soul=None, operator="", briefing=""):
        seen.append(f"{soul.name if soul else 'none'}|{briefing}")
        return chat_mod.ChatReply(text="ok")

    monkeypatch.setattr(chat_mod.HermesBridge, "ask", _ask)
    monkeypatch.setattr(chat_mod.HermesBridge, "available", property(lambda self: True))

    client = _client(tmp_path, rules_file, store)
    client.post("/chat", json={"token": TOKEN, "message": "hi", "soul": "armorer"})
    client.post("/chat", json={"token": TOKEN, "message": "hi", "soul": "yoda"})

    assert seen[0].startswith("armorer|")
    assert "account.max_risk_per_trade_pct = 1.0" in seen[0]
    assert seen[1] == "yoda|"


def test_an_unknown_soul_still_falls_back_to_the_account_agent(
    tmp_path, rules_file, store, monkeypatch
):
    """`load_soul` builds a path from this string, so it is validated against a
    fixed set. An unknown name is the wrong voice, which is a smaller failure
    than refusing the question."""
    from bot.web import chat as chat_mod

    seen: list[str] = []

    def _ask(self, message, history=None, soul=None, operator="", briefing=""):
        seen.append(soul.name if soul else "none")
        return chat_mod.ChatReply(text="ok")

    monkeypatch.setattr(chat_mod.HermesBridge, "ask", _ask)
    monkeypatch.setattr(chat_mod.HermesBridge, "available", property(lambda self: True))

    client = _client(tmp_path, rules_file, store)
    client.post("/chat", json={"token": TOKEN, "message": "hi", "soul": "../armorer"})

    assert seen == ["yoda"]


# ----------------------------------------------------------------- the CLI


def test_the_apply_command_lists_pending_requests_when_given_no_id(
    rules, rules_file, store, monkeypatch, capsys
):
    """A wrong guess at an id edits a risk limit, so the command names them
    rather than letting somebody try one. It exits non-zero so a script cannot
    read "nothing applied" as success."""
    from bot import settings_agent as module

    store.record(
        build_request(
            argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file),
            reason="Tighter.",
            rules_path=rules_file,
        )
    )
    monkeypatch.setattr(module, "SettingsRequestStore", lambda *a, **k: store)

    from bot.main import cmd_settings_apply

    code = cmd_settings_apply(None, rules_file)
    out = capsys.readouterr().out

    assert code == 1
    assert "account.max_position_pct 50.0 -> 40.0" in out
    assert "settings-apply <id>" in out


def test_the_apply_command_applies_and_prints_the_commit_message(
    rules, rules_file, store, monkeypatch, capsys
):
    from bot import settings_agent as module

    record = store.record(
        build_request(
            argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file),
            reason="Tighter, while I watch it.",
            rules_path=rules_file,
        )
    )
    monkeypatch.setattr(module, "SettingsRequestStore", lambda *a, **k: store)

    from bot.main import cmd_settings_apply

    code = cmd_settings_apply(str(record.id), rules_file)
    out = capsys.readouterr().out

    assert code == 0
    assert "config: tighten account.max_position_pct from 50.0 to 40.0" in out
    assert "Tighter, while I watch it." in out
    assert Rules.load(rules_file).account.max_position_pct == 40.0


def test_a_non_numeric_id_is_refused_before_anything_is_read(rules_file, capsys):
    from bot.main import cmd_settings_apply

    code = cmd_settings_apply("../../etc/passwd", rules_file)

    assert code == 2
    assert "is not a request id" in capsys.readouterr().out


def test_the_revert_command_puts_the_previous_value_back(
    rules, rules_file, store, capsys
):
    """A limit moved under pressure at 2am is the thing somebody wants undone at
    9am, and "it is in git" is not a route the operator has from the
    dashboard."""
    from bot.main import cmd_settings_apply

    record = store.record(
        build_request(
            argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file),
            reason="Tighter, at 2am.",
            rules_path=rules_file,
        )
    )
    assert cmd_settings_apply(str(record.id), rules_file, requests_path=store.path) == 0
    capsys.readouterr()

    code = cmd_settings_apply(
        str(record.id), rules_file, revert=True, requests_path=store.path
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "is 50.0 again" in out
    assert Rules.load(rules_file).account.max_position_pct == 50.0
    # A revert is not a change to commit, so it does not print a commit message
    # for one — the original request is still the row that describes what
    # happened.
    assert "Commit it with" not in out


def test_the_revert_command_lists_what_can_be_undone(rules, rules_file, store, capsys):
    """A different list from the pending one. Offering pending requests here
    would invite reverting an id that never moved the file."""
    from bot.main import cmd_settings_apply

    applied = store.record(
        build_request(
            argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file),
            reason="Applied one.",
            rules_path=rules_file,
        )
    )
    cmd_settings_apply(str(applied.id), rules_file, requests_path=store.path)
    store.record(
        build_request(
            argue(
                Rules.load(rules_file),
                "frequency.max_trades_per_day",
                "2",
                rules_path=rules_file,
            ),
            reason="Pending one.",
            rules_path=rules_file,
        )
    )
    capsys.readouterr()

    code = cmd_settings_apply(None, rules_file, revert=True, requests_path=store.path)
    out = capsys.readouterr().out

    assert code == 1
    assert "Applied settings requests:" in out
    assert "account.max_position_pct 50.0 -> 40.0" in out
    assert "max_trades_per_day" not in out
    assert "settings-revert <id>" in out


def test_a_dry_run_revert_proves_it_loads_and_writes_nothing(
    rules, rules_file, store, capsys
):
    from bot.main import cmd_settings_apply

    record = store.record(
        build_request(
            argue(rules, "account.max_position_pct", "40.0", rules_path=rules_file),
            reason="Tighter.",
            rules_path=rules_file,
        )
    )
    cmd_settings_apply(str(record.id), rules_file, requests_path=store.path)
    after_apply = rules_file.read_text()
    capsys.readouterr()

    code = cmd_settings_apply(
        str(record.id), rules_file, revert=True, dry_run=True, requests_path=store.path
    )

    assert code == 0
    assert "Nothing was written" in capsys.readouterr().out
    assert rules_file.read_text() == after_apply
    assert store.get(record.id).status == "applied"


def test_the_request_list_separates_pending_applied_and_reverted(
    tmp_path, rules_file, store
):
    """Reverted is not pending. A row that was applied and then put back has a
    history, and collapsing it to "not in force" loses the fact that the limit
    moved at all — which is what somebody reading this months later wants."""
    applier, _ = _stub_wrapper(store, rules_file, tmp_path)
    client = _client(tmp_path, rules_file, store, applier=applier)

    client.post(
        "/settings/request",
        json={
            "token": TOKEN,
            "key": "account.max_position_pct",
            "value": "40.0",
            "reason": "Applied and kept.",
        },
    )
    body = client.get("/settings").text
    assert "<b>Applied</b>" in body
    assert "settings-revert 1" in body
    assert "via stub" in body

    revert_request(store, 1, rules_path=rules_file, actor="stub")
    body = client.get("/settings").text

    assert "<b>Applied and then reverted</b>" in body
    assert "The limit moved and was put back" in body
