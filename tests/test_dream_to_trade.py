"""The whole chain, end to end, with no network anywhere in it.

**This chain had never once completed.** Every piece of it was built, heavily
tested and audited twice, and it could not run: `Dream.is_offerable` was defined
and never called, nothing moved a dream off the workbench, the conference read
only `Vault.VAULT`, and the model was never asked for a symbol. Three real
dreams generated against the live API came back `workbench 3/24, vault 0/12,
adopted 0/3` with `symbols: []` on every one of them, and `confer` completing
honestly with `considered: 0, calls: 0, cost 0`.

So the unit tests each side of the gaps proved their own halves and said nothing
about whether the feature worked, which is the shape of miss this file exists to
close. It walks one dream from a workbench with a met, checkable condition all
the way to an approving risk verdict on a symbol that is in no `allowed_symbols`
list anywhere:

    promote → vault → adopt → resolve_granted_symbols → RiskGate.evaluate

Everything is `tmp_path` and pure computation. No broker, no model call, no
`data/` and no `audit/`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.config import Rules
from bot.context import build_market_context
from bot.dreaming import (
    Dream,
    DreamCondition,
    DreamStore,
    DreamVerdict,
    Hop,
    Vault,
)
from bot.grants import brief_grants, resolve_granted_symbols, resolve_grants
from bot.models import (
    AccountSnapshot,
    Direction,
    IndicatorSnapshot,
    OrderProposal,
    Tick,
    TriggerField,
    TriggerOp,
)
from bot.risk import RiskGate
from bot.triggers import CycleReadings

from .conftest import INSIDE_SESSION, PAPER_EQUITY, RULES_PATH

# A US equity that is in NO `allowed_symbols` list in config/rules.yaml. The
# whole point of the feature is that a dream can reach one of these.
GRANTED_SYMBOL = "AA"

# Before the market opens on the day the gate is asked, so the grant is live and
# the readings that fired the condition are genuinely earlier.
YESTERDAY = INSIDE_SESSION - timedelta(days=1)


@pytest.fixture
def rules() -> Rules:
    loaded = Rules.load(RULES_PATH)
    assert loaded.dreaming.allow_symbol_grants, (
        "config/rules.yaml is expected to enable symbol grants."
    )
    assert GRANTED_SYMBOL not in loaded.allowed_symbols, (
        f"{GRANTED_SYMBOL} must NOT be in allowed_symbols, or this test proves "
        "nothing about a dream widening what may be traded."
    )
    return loaded


@pytest.fixture
def store(tmp_path) -> DreamStore:
    return DreamStore(tmp_path / "dreams.db")


def _dream() -> Dream:
    """A finished dream with one pre-registered, checkable, not-yet-met condition."""
    return Dream(
        title="Data centres and the smelters on the same grid",
        seed="Data centres bid for the power aluminium smelters run on.",
        verdict=DreamVerdict.KEEP,
        chain=[
            Hop("Smelters buy power on long interconnect contracts.", True, "utility filing"),
            Hop("The largest US smelter sits inside that region.", False, ""),
            Hop("Alcoa is the listed instrument that exposure reaches.", False, ""),
        ],
        # The hop the chain rests on, as a sentence AND as a number. Hop 3 is the
        # bridge from an unlisted mechanism to a ticker, which is the link most
        # likely to be wrong and the one nobody checks — and the condition below
        # is pinned to it, which is what buys this dream a place on the prophecy
        # shelf at all. Pin it to hop 1 instead and `promotion_for` refuses:
        # a threshold on a link nobody doubted settles nothing.
        weakest_hop="that the exposure actually reaches Alcoa rather than a private smelter",
        weakest_hop_index=3,
        instruments=["aluminium", "grid power", "data centres"],
        symbols=[GRANTED_SYMBOL],
        asset_class_key="us_equity",
        conditions=[
            DreamCondition(
                text="Alcoa closes above 100, confirming the market has noticed",
                symbol=GRANTED_SYMBOL,
                field=TriggerField.CLOSE,
                op=TriggerOp.ABOVE,
                value=100.0,
                settles_hops=(3,),
            )
        ],
    )


def _fired() -> list[CycleReadings]:
    """What the decision loop recorded, as `MarketInputs.readings` stores it."""
    return [
        CycleReadings(
            at=YESTERDAY - timedelta(hours=2),
            readings={GRANTED_SYMBOL: IndicatorSnapshot(close=94.0)},
        ),
        CycleReadings(
            at=YESTERDAY,
            readings={GRANTED_SYMBOL: IndicatorSnapshot(close=104.5)},
        ),
    ]


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity_usd=PAPER_EQUITY,
        cash_usd=PAPER_EQUITY,
        buying_power_usd=PAPER_EQUITY,
        open_positions=[],
    )


def _proposal() -> OrderProposal:
    """3 shares at $104 — $312 notional, $15 of risk. Inside every cap."""
    return OrderProposal(
        symbol=GRANTED_SYMBOL,
        direction=Direction.BUY,
        qty=3,
        limit_price=104.00,
        stop_loss_price=99.00,
        take_profit_price=115.00,
        rationale="Held the breakout level on the session's figures; wrong below 99.",
    )


def test_a_dream_walks_from_the_workbench_to_an_approved_order(rules, store):
    """The chain, in the order it actually happens.

    Each assertion is a stage that was previously either unreachable or
    unconnected to the next one.
    """
    dream_id = store.save(_dream())
    tick = Tick(
        symbol=GRANTED_SYMBOL, bid=103.98, ask=104.02, timestamp=INSIDE_SESSION
    )

    # 1. Promotion. A keep with a checkable but unmet condition is a PROPHECY,
    #    never straight into the vault: the claim has not come true yet.
    first = store.promote(dream_id, at=YESTERDAY - timedelta(days=1))
    assert first.ok and first.moved_to is Vault.PROPHECY

    # 2. Grading against what the loop recorded. The condition fires on the
    #    first cycle that meets it, not the latest.
    grading = store.grade(dream_id, _fired(), at=YESTERDAY)
    assert grading.changed
    assert grading.conditions[0].fulfilled_at == YESTERDAY

    # 3. Promotion again, now that every condition is met: into the vault, the
    #    only shelf the trading agent can see.
    second = store.promote(dream_id, at=YESTERDAY)
    assert second.ok and second.moved_to is Vault.VAULT
    offered = store.in_vault(Vault.VAULT)
    assert [d.id for d in offered] == [dream_id]
    assert offered[0].is_offerable is True

    # 4. Adoption. This is the trading agent's own power and the only thing that
    #    turns a dream into a permission.
    adopted = store.adopt(dream_id, at=YESTERDAY)
    assert adopted.ok, adopted.details

    # 5. Resolution, outside the gate, exactly as the loop does it.
    granted = resolve_granted_symbols(store, rules, now=INSIDE_SESSION)
    assert granted == {GRANTED_SYMBOL: "us_equity"}

    # 6. The gate. A symbol in no allowed_symbols list anywhere, approved.
    verdict = RiskGate(
        rules, equity_at_session_start=PAPER_EQUITY, now=INSIDE_SESSION
    ).evaluate(
        _proposal(),
        account=_account(),
        tick=tick,
        granted_symbols=granted,
    )

    assert verdict.approved, verdict.reasons
    # And the trade is marked as owing its existence to a dream, so the journal
    # can record the provenance and the Board can render the tag.
    assert verdict.permitted_by_dream_grant is True
    assert verdict.granted_by_dream_class == "us_equity"


def test_the_same_proposal_is_refused_the_moment_the_dream_is_handed_back(
    rules, store
):
    """The permission is the dream, and nothing else.

    Without this the test above would pass for a gate that had simply stopped
    checking `allowed_symbols`. `return_to_vault` is one of the trading agent's
    two powers, and the refusal has to arrive with it.
    """
    dream_id = store.save(_dream())
    store.promote(dream_id, at=YESTERDAY - timedelta(days=1))
    store.grade(dream_id, _fired(), at=YESTERDAY)
    store.promote(dream_id, at=YESTERDAY)
    store.adopt(dream_id, at=YESTERDAY)

    store.return_to_vault(dream_id, reason="the power contract was re-signed", at=YESTERDAY)

    granted = resolve_granted_symbols(store, rules, now=INSIDE_SESSION)
    assert granted == {}

    verdict = RiskGate(
        rules, equity_at_session_start=PAPER_EQUITY, now=INSIDE_SESSION
    ).evaluate(
        _proposal(),
        account=_account(),
        tick=Tick(
            symbol=GRANTED_SYMBOL, bid=103.98, ask=104.02, timestamp=INSIDE_SESSION
        ),
        granted_symbols=granted,
    )

    assert not verdict.approved
    assert any("not in the allowed" in r.lower() for r in verdict.reasons), verdict.reasons


def test_the_granted_symbol_reaches_the_prompt_labelled(rules, store):
    """A permission the model is never told about is a permission nothing uses.

    This is the half that made the feature inert even with the gate correct: the
    prompt listed `rules.allowed_symbols` and every feed ran over the same list,
    so a granted symbol had no quote, no indicators and no chance of being
    proposed.
    """
    dream_id = store.save(_dream())
    store.promote(dream_id, at=YESTERDAY - timedelta(days=1))
    store.grade(dream_id, _fired(), at=YESTERDAY)
    store.promote(dream_id, at=YESTERDAY)
    store.adopt(dream_id, at=YESTERDAY)

    resolution = resolve_grants(store, rules, now=INSIDE_SESSION)
    briefing = brief_grants(store, resolution, now=INSIDE_SESSION)

    blob = build_market_context(
        account=_account(),
        ticks={
            GRANTED_SYMBOL: Tick(
                symbol=GRANTED_SYMBOL,
                bid=103.98,
                ask=104.02,
                timestamp=INSIDE_SESSION,
            )
        },
        headlines=[],
        news_windows=[],
        grants=briefing,
        now=INSIDE_SESSION,
    )

    # The symbol, its class and when the permission ends.
    assert f"{GRANTED_SYMBOL} (us_equity)" in blob
    assert "permission ends" in blob
    # The chain, and never without what qualifies it.
    assert "PARTIAL" in blob
    assert "WEAKEST HOP: that the exposure actually reaches Alcoa" in blob
    assert "hop 2 (UNCHECKED)" in blob
    # And the rule that keeps a permission from reading as a recommendation.
    assert "does NOT propose a position" in blob


def test_the_widened_symbol_set_is_what_the_feeds_would_run_over(rules, store):
    """The arithmetic `cmd_loop` does before it fetches anything.

    Kept as its own assertion because the ordering is the bug: resolving the
    grant AFTER the feeds — which is where it used to sit — leaves the symbol
    without a tick, and a proposal in it is dropped for want of a quote before
    it ever reaches the gate that would have allowed it.
    """
    dream_id = store.save(_dream())
    store.promote(dream_id, at=YESTERDAY - timedelta(days=1))
    store.grade(dream_id, _fired(), at=YESTERDAY)
    store.promote(dream_id, at=YESTERDAY)
    store.adopt(dream_id, at=YESTERDAY)

    granted = resolve_granted_symbols(store, rules, now=INSIDE_SESSION)
    symbols_in_play = sorted(set(rules.allowed_symbols) | set(granted))

    assert GRANTED_SYMBOL in symbols_in_play
    assert set(rules.allowed_symbols).issubset(symbols_in_play)


def test_a_dream_naming_a_disabled_class_never_completes_the_chain(rules, store):
    """The hard block, proved on the same path rather than in isolation.

    Crypto is configured and disabled. A dream may widen the symbols inside an
    enabled class and can never enable a class, so this one promotes, vaults and
    adopts exactly as above and grants nothing at the end of it.
    """
    assert rules.instruments["crypto"].enabled is False
    crypto = _dream()
    crypto.symbols = ["BTC/USD"]
    crypto.asset_class_key = "crypto"
    crypto.conditions[0] = DreamCondition(
        text="Bitcoin closes above 100,000",
        symbol="BTC/USD",
        field=TriggerField.CLOSE,
        op=TriggerOp.ABOVE,
        value=100_000.0,
        # Pinned to the chain's weakest hop, like the one it replaces. Without
        # it this dream would never leave the workbench and the test would prove
        # the promotion rule rather than the class hard block it is here for.
        settles_hops=(3,),
        fulfilled=True,
    )
    dream_id = store.save(crypto)

    assert store.promote(dream_id, at=YESTERDAY).moved_to is Vault.VAULT
    assert store.adopt(dream_id, at=YESTERDAY).ok

    assert resolve_granted_symbols(store, rules, now=INSIDE_SESSION) == {}
