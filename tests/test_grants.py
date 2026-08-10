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
from bot.grants import resolve_grant_dream_ids, resolve_granted_symbols

from .conftest import RULES_PATH

NOW = datetime(2026, 6, 1, tzinfo=UTC)


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
    store = _Store({"TSLA": "us_equity", "NVDA": "us_equity", "GME": "us_equity"})

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
    store = _Store({"TSLA": "us_equity", "NVDA": "us_equity"})

    assert len(resolve_granted_symbols(store, rules, now=NOW)) == 2


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
