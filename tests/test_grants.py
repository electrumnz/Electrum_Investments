"""The seam between the dream store and the risk gate.

This is the file that decides what a dream is allowed to widen, so every rule in
it gets a test that proves it REFUSES rather than one that proves it exists. The
four that matter:

- a grant naming a disabled instrument class grants nothing,
- a symbol the allowlist already permits is not reported as a grant,
- any failure at all grants nothing,
- over the cap, nothing is granted rather than an arbitrary subset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import Rules
from bot.dreaming import DREAMER, Adoption, Dream, DreamStore, Vault
from bot.grants import (
    GRANTED,
    NONE_LIVE,
    OVER_CAP,
    SWITCHED_OFF,
    UNAVAILABLE,
    GrantResolution,
    brief_grants,
    resolve_grant_dream_ids,
    resolve_granted_symbols,
    resolve_grants,
)

from .conftest import RULES_PATH

NOW = datetime(2026, 6, 1, tzinfo=UTC)

#: Symbols these tests use to stand for "not in `config/rules.yaml`".
#:
#: A grant naming a symbol the allowlist ALREADY carries widens nothing, and
#: `_resolve` drops it as redundant — correctly, because a listed symbol a stale
#: grant also names is an ordinary trade. The consequence is that a cap test
#: built on such a symbol stops testing the cap and passes anyway, which is
#: precisely what happened when `us_equity.allowed_symbols` grew from six names
#: to twenty and NVDA became listed.
#:
#: So they are named once, and `test_the_unlisted_symbols_really_are_unlisted`
#: fails the build the next time the allowlist reaches one of them, instead of
#: letting a rule quietly stop being exercised.
UNLISTED = ("TSLA", "RIVN", "GME")


@pytest.fixture
def rules() -> Rules:
    """The shipped rules, with grants switched on as the file ships them."""
    loaded = Rules.load(RULES_PATH)
    assert loaded.dreaming.allow_symbol_grants, (
        "config/rules.yaml is expected to enable symbol grants; these tests "
        "cover what happens when it does."
    )
    return loaded


class _Store:
    """A dream store stand-in. Counts its reads and can be made to fail.

    The real `DreamStore.granted_symbols` catches its own `sqlite3.Error` and
    returns `{}`, so the outer guard in `resolve_granted_symbols` cannot be
    exercised against it at all — and an untested fail-closed path is one nobody
    has watched fail.
    """

    def __init__(
        self,
        granted: dict[str, str] | None = None,
        *,
        adoptions: list[Adoption] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._granted = granted or {}
        self._adoptions = adoptions or []
        self._error = error
        self.reads = 0

    def granted_symbols(self, now: datetime) -> dict[str, str]:
        self.reads += 1
        if self._error is not None:
            raise self._error
        return dict(self._granted)

    def adoptions(self, dream_id: int | None = None) -> list[Adoption]:
        self.reads += 1
        if self._error is not None:
            raise self._error
        return list(self._adoptions)

    def get(self, dream_id: int) -> Dream | None:
        """`brief_grants` reads the dream behind a grant for its chain."""
        self.reads += 1
        if self._error is not None:
            raise self._error
        return None


def _adoption(
    dream_id: int,
    symbols: list[str],
    *,
    asset_class: str = "us_equity",
    returned: bool = False,
    expires_in_days: int | None = 90,
) -> Adoption:
    return Adoption(
        dream_id=dream_id,
        adopted_at=NOW,
        symbols_granted=symbols,
        asset_class=asset_class,
        returned_at=NOW if returned else None,
        expires_at=(
            NOW + timedelta(days=expires_in_days) if expires_in_days is not None else None
        ),
    )


# ------------------------------------------------------------- the happy path


def test_a_granted_symbol_is_reported_with_the_class_its_limits_come_from(rules):
    """The class is the load-bearing half.

    `Rules.for_symbol` cannot find a granted symbol — it is in no
    `allowed_symbols` list — so without the class key the gate would have no
    limits to apply to it.
    """
    store = _Store({"TSLA": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {"TSLA": "us_equity"}


def test_the_real_store_satisfies_the_protocol_end_to_end(rules, tmp_path):
    """Structural typing is checked by mypy; this checks it at runtime.

    A protocol that drifted from `DreamStore` — a renamed keyword, a changed
    return shape — would type-check against the stub above and fail against the
    only implementation that matters.
    """
    store = DreamStore(tmp_path / "dreams.db")
    dream_id = store.save(
        Dream(title="t", seed="s", symbols=["TSLA"], asset_class_key="us_equity")
    )
    assert store.move(dream_id, Vault.VAULT, by=DREAMER)
    assert store.adopt(dream_id, at=NOW).ok

    granted = resolve_granted_symbols(store, rules, now=NOW + timedelta(days=1))
    assert granted == {"TSLA": "us_equity"}
    assert resolve_grant_dream_ids(
        store, granted, now=NOW + timedelta(days=1)
    ) == {"TSLA": dream_id}


# ------------------------------------------------------- the class hard block


def test_a_grant_under_a_disabled_class_grants_nothing(rules):
    """Crypto off means off, not "off unless a dream says otherwise".

    The operator's rule: the dreamer may look outside `allowed_symbols` to other
    Alpaca instruments, and may never go around a hard block on a GROUP of
    instruments. The block is derived from `enabled_instruments`, so switching
    a class off withdraws every grant under it in the same edit.
    """
    assert rules.instruments["crypto"].enabled is False
    store = _Store({"BTC/USD": "crypto"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {}


def test_enabling_the_class_is_what_makes_the_same_grant_land(rules):
    """The other half of the rule, so the test is about the block and not about
    the symbol."""
    store = _Store({"BTC/USD": "crypto"})
    rules.instruments["crypto"].enabled = True
    rules.instruments["crypto"].allowed_symbols = ["ETH/USD"]

    assert resolve_granted_symbols(store, rules, now=NOW) == {"BTC/USD": "crypto"}


def test_a_grant_naming_a_class_nobody_configured_grants_nothing(rules):
    """A key that matches no instrument block is a symbol whose limits are
    unknown, and an unknown is never treated as a permission."""
    store = _Store({"ES": "futures"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {}


def test_one_dropped_grant_does_not_drop_the_others(rules):
    """The block is per grant, not per resolution. A dream reaching for crypto
    must not cost an unrelated, valid equity grant."""
    store = _Store({"BTC/USD": "crypto", "TSLA": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {"TSLA": "us_equity"}


# --------------------------------------------------- a grant that grants nothing


def test_a_symbol_already_on_the_allowlist_is_not_reported_as_a_grant(rules):
    """It is already permitted, so the dream is not what lets it through.

    Reporting it would put a dream's provenance on a trade `config/rules.yaml`
    would have allowed anyway, and the audit trail would claim the dream was
    load-bearing when it was not.
    """
    assert "SPY" in rules.allowed_symbols
    store = _Store({"SPY": "us_equity", "TSLA": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {"TSLA": "us_equity"}


# ------------------------------------------------------------ failing closed


def test_any_failure_grants_nothing(rules):
    """A database error, a torn row, a type nobody expected: the answer is that
    nothing is granted, never a partial mapping presented as complete."""
    store = _Store({"TSLA": "us_equity"}, error=RuntimeError("database is locked"))

    assert resolve_granted_symbols(store, rules, now=NOW) == {}


def test_a_failure_is_not_an_exception_into_the_decision_path(rules):
    """It must not raise. This runs inside the cycle that reconciles the journal
    and watches open stops, and an exception here would end the loop."""
    store = _Store(error=KeyError("something nobody anticipated"))

    resolve_granted_symbols(store, rules, now=NOW)  # no raise is the assertion


def test_the_switch_off_grants_nothing_and_never_reads_the_store(rules):
    """`allow_symbol_grants` defaults to False in the model, so a config that
    has never heard of this feature fails closed."""
    rules.dreaming.allow_symbol_grants = False
    store = _Store({"TSLA": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {}
    assert store.reads == 0, "a switched-off feature must not touch the store"


def test_a_default_rules_object_grants_nothing(rules):
    """The asymmetry stated as a test: the model defaults to off and the shipped
    file turns it on. A `Rules` built without a `dreaming:` block — every test
    fixture, every older deployment — grants nothing at all."""
    bare = Rules(
        account=rules.account,
        frequency=rules.frequency,
        news_blackout_minutes_before=0,
        news_blackout_minutes_after=0,
        instruments=rules.instruments,
        watchlist=rules.watchlist,
    )

    assert bare.dreaming.allow_symbol_grants is False
    assert resolve_granted_symbols(_Store({"TSLA": "us_equity"}), bare, now=NOW) == {}


# -------------------------------------------------------------------- the cap


def test_over_the_cap_nothing_is_granted_rather_than_a_subset(rules):
    """An arbitrary subset is a permission set nobody can predict.

    Which is worse than none: it would change from cycle to cycle with the
    ordering of a query, and no operator could say in advance what the bot was
    allowed to trade.
    """
    rules.dreaming.max_granted_symbols = 2
    store = _Store(dict.fromkeys(UNLISTED, "us_equity"))

    assert resolve_granted_symbols(store, rules, now=NOW) == {}


def test_the_cap_measures_what_is_actually_widened(rules):
    """Counted after the drops, because a grant that widens nothing is not a
    permission. Four live grants of which three are already allowed is one new
    symbol, and refusing that against a cap of two would be arithmetic on the
    wrong number."""
    rules.dreaming.max_granted_symbols = 2
    store = _Store(
        {"SPY": "us_equity", "QQQ": "us_equity", "AAPL": "us_equity", "TSLA": "us_equity"}
    )

    assert resolve_granted_symbols(store, rules, now=NOW) == {"TSLA": "us_equity"}


def test_exactly_at_the_cap_is_allowed(rules):
    rules.dreaming.max_granted_symbols = 2
    store = _Store(dict.fromkeys(UNLISTED[:2], "us_equity"))

    assert len(resolve_granted_symbols(store, rules, now=NOW)) == 2


def test_the_unlisted_symbols_really_are_unlisted(rules):
    """The guard on the two cap tests above, and on nothing else.

    Both of them measure what a grant WIDENS, so both are silently disarmed by a
    symbol the allowlist has since grown to include. This states the assumption
    where it can fail rather than leaving it in a fixture nobody re-reads.
    """
    for symbol in UNLISTED:
        assert not rules.is_symbol_allowed(symbol), (
            f"{symbol} is now in config/rules.yaml, so a grant naming it widens "
            "nothing and the cap tests above stop exercising the cap. Pick "
            "another symbol for UNLISTED."
        )


# ------------------------------------------------------------- the provenance


def test_provenance_names_the_dream_behind_each_granted_symbol():
    store = _Store(adoptions=[_adoption(7, ["TSLA"]), _adoption(9, ["NVDA"])])

    ids = resolve_grant_dream_ids(
        store, {"TSLA": "us_equity", "NVDA": "us_equity"}, now=NOW
    )

    assert ids == {"TSLA": 7, "NVDA": 9}


def test_a_symbol_two_live_dreams_claim_carries_no_dream_id():
    """There is no correct answer to which dream it came from.

    Writing one of them would put a plausible wrong provenance in the journal,
    and the symbol is still granted — losing the id costs a tag, where losing
    the permission would change what may be traded.
    """
    store = _Store(adoptions=[_adoption(7, ["TSLA"]), _adoption(9, ["TSLA"])])

    assert resolve_grant_dream_ids(store, {"TSLA": "us_equity"}, now=NOW) == {}


def test_a_returned_or_expired_adoption_supplies_no_provenance():
    store = _Store(
        adoptions=[
            _adoption(7, ["TSLA"], returned=True),
            _adoption(9, ["NVDA"], expires_in_days=1),
        ]
    )

    ids = resolve_grant_dream_ids(
        store, {"TSLA": "us_equity", "NVDA": "us_equity"}, now=NOW + timedelta(days=30)
    )

    assert ids == {}


def test_provenance_ignores_symbols_that_were_not_granted():
    """An adoption naming a symbol the resolver dropped — a disabled class, say
    — must not reappear here."""
    store = _Store(adoptions=[_adoption(7, ["TSLA", "BTC/USD"], asset_class="crypto")])

    assert resolve_grant_dream_ids(store, {"TSLA": "us_equity"}, now=NOW) == {"TSLA": 7}


def test_provenance_failure_costs_the_id_and_not_the_cycle():
    store = _Store(error=RuntimeError("database is locked"))

    assert resolve_grant_dream_ids(store, {"TSLA": "us_equity"}, now=NOW) == {}


def test_no_grants_means_no_query():
    store = _Store(adoptions=[_adoption(7, ["TSLA"])])

    assert resolve_grant_dream_ids(store, {}, now=NOW) == {}
    assert store.reads == 0


# ---------------------------------------- the hard block, checked on the SYMBOL


def test_a_grant_claiming_the_wrong_class_for_its_symbol_grants_nothing(rules):
    """The hole an adversarial audit walked through, and the reason it mattered.

    The block used to ask only whether the class key an adoption row NAMED was
    an enabled one. `BTC/USD` under `us_equity` answered yes, so crypto — the
    class the operator switched off — was tradeable under the equity book's
    limits: 1% per-trade risk instead of 0.5%, 50% concentration instead of 15%,
    three positions instead of one.

    And the limits are the smaller half. `AlpacaBroker.place_order` routes on
    `"/" in symbol`, so the order that reached Alpaca was a CRYPTO order —
    unbracketed, because Alpaca accepts no bracket there — which means no
    broker-side stop rested behind it at all. That is the operator's third rule,
    gone, through a permission that named the wrong word.
    """
    store = _Store({"BTC/USD": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {}


def test_the_same_symbol_under_its_own_class_is_refused_by_the_class_block(rules):
    """Both halves refuse it, and they refuse it for different reasons.

    Naming crypto is caught by the enabled check; naming us_equity is caught by
    the symbol check. A test that only tried the first would pass over the
    second, which is exactly what happened.
    """
    assert rules.instruments["crypto"].enabled is False

    assert resolve_granted_symbols(_Store({"BTC/USD": "crypto"}), rules, now=NOW) == {}
    assert resolve_granted_symbols(_Store({"BTC/USD": "us_equity"}), rules, now=NOW) == {}


def test_a_symbol_a_disabled_class_LISTS_cannot_be_granted_under_another(rules):
    """`Rules.for_symbol` scans enabled classes only, so a disabled class's
    `allowed_symbols` was invisible to every check. It is the operator's own
    statement of which class a symbol belongs to and it has to be read."""
    rules.instruments["crypto"].allowed_symbols = ["WIDGET"]
    assert rules.instruments["crypto"].enabled is False

    assert resolve_granted_symbols(_Store({"WIDGET": "us_equity"}), rules, now=NOW) == {}


def test_a_symbol_two_blocks_both_claim_grants_nothing(rules):
    """Where the file and the file disagree there is no single answer, and an
    unknown is never treated as a permission.

    Both claimants are disabled on purpose, so the symbol is in no enabled
    `allowed_symbols` list and the "already permitted" rule cannot be what
    refuses it. What refuses it is that its class cannot be established.
    """
    rules.instruments["crypto"].allowed_symbols = ["WIDGET"]
    rules.instruments["shelved"] = rules.instruments["crypto"].model_copy(
        update={"allowed_symbols": ["WIDGET"], "enabled": False}
    )

    assert resolve_granted_symbols(_Store({"WIDGET": "us_equity"}), rules, now=NOW) == {}


def test_an_ordinary_ticker_still_resolves_under_the_equity_class(rules):
    """The check must not refuse the case the whole feature exists for: a symbol
    nobody has listed anywhere, which is what a dreamer is allowed to reach
    for."""
    assert resolve_granted_symbols(_Store({"MP": "us_equity"}), rules, now=NOW) == {
        "MP": "us_equity"
    }


def test_the_wrong_class_costs_that_grant_and_no_other(rules):
    store = _Store({"BTC/USD": "us_equity", "MP": "us_equity"})

    assert resolve_granted_symbols(store, rules, now=NOW) == {"MP": "us_equity"}


# --------------------------------------------- why the mapping is empty is said


def test_an_empty_mapping_says_which_of_its_five_causes_it_was(rules):
    """`calendar_degraded` again: a zero has to be a stated fact rather than the
    absence of a warning. Switched off, nothing adopted, a broken store and a
    set over the cap all render as `[]` on the cycle line."""
    off = Rules.load(RULES_PATH)
    off.dreaming.allow_symbol_grants = False

    assert resolve_grants(_Store({}), off, now=NOW).state == SWITCHED_OFF
    assert resolve_grants(_Store({}), rules, now=NOW).state == NONE_LIVE
    assert resolve_grants(_Store({"MP": "us_equity"}), rules, now=NOW).state == GRANTED
    assert (
        resolve_grants(_Store(error=RuntimeError("locked")), rules, now=NOW).state
        == UNAVAILABLE
    )
    over = {f"SYM{i}": "us_equity" for i in range(rules.dreaming.max_granted_symbols + 1)}
    assert resolve_grants(_Store(over), rules, now=NOW).state == OVER_CAP


def test_only_the_states_that_withhold_an_answer_read_as_degraded(rules):
    """A feature nobody turned on is a configuration, not a failure. Flagging it
    would make every deployment that does not use dreams look broken, and a
    warning that is always on is a warning nobody reads."""
    off = Rules.load(RULES_PATH)
    off.dreaming.allow_symbol_grants = False

    assert resolve_grants(_Store({}), off, now=NOW).degraded is False
    assert resolve_grants(_Store({}), rules, now=NOW).degraded is False
    assert resolve_grants(_Store({"MP": "us_equity"}), rules, now=NOW).degraded is False
    assert resolve_grants(_Store(error=RuntimeError("x")), rules, now=NOW).degraded is True
    over = {f"SYM{i}": "us_equity" for i in range(rules.dreaming.max_granted_symbols + 1)}
    assert resolve_grants(_Store(over), rules, now=NOW).degraded is True


# ------------------------------------------- an exception must not reach a loop


def test_a_naive_now_costs_the_provenance_and_never_the_cycle():
    """The guard used to wrap the QUERY and not the arithmetic after it.

    `Adoption.is_live` compares datetimes, so a naive `now` raised `TypeError`
    straight out of `resolve_grant_dream_ids` and into the decision cycle — the
    shape `claude.propose` was wrapped for after a `ValidationError` killed a
    live loop and systemd restarted it into the same failure.
    """
    store = _Store(adoptions=[_adoption(7, ["TSLA"])])

    ids = resolve_grant_dream_ids(
        store, {"TSLA": "us_equity"}, now=NOW.replace(tzinfo=None)
    )

    assert ids == {"TSLA": 7}


def test_an_adoption_row_that_will_not_iterate_costs_the_provenance_only():
    """Anything unexpected from the store, not merely a raise from the query."""
    broken = Adoption(
        dream_id=7,
        adopted_at=NOW,
        symbols_granted=None,  # type: ignore[arg-type]
        asset_class="us_equity",
        expires_at=NOW + timedelta(days=30),
    )
    store = _Store(adoptions=[broken])

    assert resolve_grant_dream_ids(store, {"TSLA": "us_equity"}, now=NOW) == {}


# ------------------------------------------------------- the model's briefing
#
# A third question, kept apart from the other two. `resolve_grants` answers what
# may be traded, `resolve_grant_dream_ids` answers what to journal it against,
# and this answers what the reasoner is told — which is worth nothing to either
# of the others and must never be able to change what they say.


def test_the_briefing_carries_the_chain_and_the_expiry_behind_each_grant(
    rules, tmp_path
):
    store = DreamStore(tmp_path / "dreams.db")
    dream_id = store.save(
        Dream(
            title="Smelters and power",
            seed="s",
            symbols=["TSLA"],
            asset_class_key="us_equity",
        )
    )
    store.move(dream_id, Vault.VAULT, by=DREAMER)
    store.adopt(dream_id, at=NOW)

    resolution = resolve_grants(store, rules, now=NOW + timedelta(days=1))
    briefing = brief_grants(store, resolution, now=NOW + timedelta(days=1))

    assert briefing.symbols == {"TSLA": "us_equity"}
    assert briefing.dream_ids == {"TSLA": dream_id}
    assert [d.id for d in briefing.dreams] == [dream_id]
    assert briefing.expires_at[dream_id] == NOW + timedelta(days=90)
    assert briefing.chains_available is True


def test_a_briefing_failure_costs_the_CHAIN_and_never_the_PERMISSION(rules):
    """**The failure direction, and it is the opposite of everywhere else here.**

    Every other path in this module fails closed to an empty mapping, because an
    unknown must never be treated as a permission. This one must not: the
    symbols come from the resolution the GATE has already been handed, so
    dropping them here would leave the gate permitting something the model was
    never told about — which is exactly the inert state this whole feature
    exists to leave behind. Losing the chain is the smaller failure.
    """
    store = _Store({"TSLA": "us_equity"}, error=RuntimeError("store is gone"))
    resolution = GrantResolution(symbols={"TSLA": "us_equity"}, state=GRANTED)

    briefing = brief_grants(store, resolution, now=NOW)

    assert briefing.symbols == {"TSLA": "us_equity"}
    assert briefing.dreams == ()
    assert briefing.chains_available is False


def test_nothing_granted_means_nothing_is_read(rules):
    """An ordinary cycle pays nothing for a feature it is not using."""
    store = _Store({})

    briefing = brief_grants(store, GrantResolution(state=NONE_LIVE), now=NOW)

    assert briefing.has_grants is False
    assert store.reads == 0
