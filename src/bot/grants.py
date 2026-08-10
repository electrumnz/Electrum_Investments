"""The seam between the dream store and the risk gate.

One job: turn what `DreamStore.granted_symbols` says is permitted right now into
something `RiskGate.evaluate` can be handed, applying the class hard block and
failing closed on anything unexpected.

**It exists so that the gate does not have to.** `RiskGate` must stay
deterministic — no SQLite read, no network, no clock beyond its own `_now()` —
because a gate that can fail is a gate that can fail OPEN, and this one is the
only thing between a confidently wrong model and the account. So the grant is
resolved out here, where a failure costs a permission rather than a rule, and
handed in as a plain mapping in the same shape as `news_windows`. Nothing in
`risk.py` imports this module and nothing in this module imports `risk`.

## The operator's rule, and the five ways this enforces it

*The dreamer may look outside `allowed_symbols` to other Alpaca instruments, as
long as it does not go around the hard blocks on GROUPS of instruments.*

- **The class hard block is derived from `Rules.enabled_instruments`**, never
  from `allowed_symbols`. A grant whose class key names a disabled — or unknown
  — class is DROPPED. Crypto off means an adopted dream naming BTC/USD grants
  nothing, whatever the dream says. That is what makes "off" mean off rather
  than "off unless a dream says otherwise".
- **And the block is checked against the SYMBOL, not against the key the
  adoption claims.** This is the half that was missing, and an adversarial audit
  walked straight through it: the code asked only "is the class this row names
  an enabled one?", so an adoption saying `BTC/USD` under `us_equity` was a live
  permission to trade crypto under the equity book's limits — 0.5% risk cap, 15%
  concentration and one-position rules all bypassed — and because
  `AlpacaBroker.place_order` routes on `"/" in symbol`, the order that reached
  Alpaca *was* a crypto order: unbracketed, so **no broker-side stop**, which is
  the operator's third rule gone. `Rules.true_class_key` now derives the real
  class from every instrument block's `allowed_symbols`, enabled or not, and
  from `models.is_crypto_symbol` — the same rule the broker routes on. A claim
  that disagrees with it is dropped, and so is a symbol the two cannot agree on.
  The fence and the router are one definition now, because two of them is
  exactly how they came to disagree.
- **A symbol already in `rules.allowed_symbols` is not a grant.** It is already
  permitted, and reporting it as granted would make the audit trail claim a
  dream was load-bearing on a trade that would have happened anyway.
- **Any failure yields an EMPTY mapping.** A database error, a missing file, a
  torn row, a type nobody expected: the answer is "nothing is granted", never a
  partial mapping presented as complete and never an exception into a decision
  path. Same rule as `FinnhubCalendar.is_degraded` and `stops_unchecked` — an
  unknown is never treated as a permission. The account carries on trading
  exactly what `config/rules.yaml` already allows.
- **Over the cap, nothing is granted.** `dreaming.max_granted_symbols` bounds
  the set; more live grants than that and the answer is none, logged loudly.
  Taking an arbitrary subset would be a permission set nobody can predict, and
  a permission nobody can predict is worse than none.

And one that is not a rule so much as the shape of the thing:
`allow_symbol_grants` is checked FIRST, so a deployment that has not turned the
feature on never reads the store at all.

## Provenance is a second question, answered separately

`resolve_granted_symbols` returns symbol → instrument-class key, because the
class is what the gate needs: it is how a granted symbol gets limits at all.
That mapping cannot say WHICH dream permitted a symbol, and the journal wants
exactly that — `Trade.dream_id` is the provenance the Board renders and the only
thing that makes a `dream` tag derivable from stored state.

So `resolve_grant_dream_ids` answers it, off the same store and with the same
failure direction. It is deliberately not folded into the first function: the
gate must not be handed a dream id it has no use for, and a caller that only
wants to know what may be traded should not pay for a second query.

## An empty mapping has five causes, and the caller is told which

Failing closed is right and it costs the caller information: the feature
switched off, nothing adopted, a broken store, an unreadable row and a set over
the cap all produce `{}`. On a `cycle_complete` line they are one blank list.
That is the `calendar_degraded` and `stops_unchecked` lesson — a zero has to be
a stated fact rather than the absence of a warning — so `resolve_grants` returns
a `GrantResolution` naming its state, and the loop puts `grants_degraded` on the
heartbeat beside the other two. `resolve_granted_symbols` is the mapping-only
wrapper, for callers with nowhere to report a degradation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import structlog

from .config import Rules
from .dreaming import Adoption, Dream

log = structlog.get_logger(__name__)


# What the resolver could not do, in one word each. `granted` and `none_live`
# are ordinary answers; the other three mean the mapping is not a complete
# description of what is adopted.
GRANTED = "granted"
NONE_LIVE = "none_live"
SWITCHED_OFF = "switched_off"
UNAVAILABLE = "unavailable"
OVER_CAP = "over_cap"

# Public, because the loop has to answer the same question for the case where
# there is no store to ask at all — and two spellings of "is this degraded"
# is how the heartbeat and the resolver come to disagree.
DEGRADED_STATES = frozenset({UNAVAILABLE, OVER_CAP})


@dataclass(frozen=True)
class GrantResolution:
    """What may be traded beyond `allowed_symbols`, and how much to trust it.

    `symbols` is the whole permission and is safe to use on its own — every
    failure path leaves it empty. `state` exists because empty has five causes
    and only two of them are ordinary, and a reader handed a blank list reaches
    for the wrong one every time.
    """

    symbols: dict[str, str] = field(default_factory=dict)
    state: str = NONE_LIVE

    @property
    def degraded(self) -> bool:
        """True when `symbols` is not a complete description of what is adopted.

        Deliberately False for `switched_off`: a feature nobody turned on is a
        stated configuration rather than a failure, and flagging it would make
        every deployment that does not use dreams look broken. It is still named
        in `state`, so the two are distinguishable on the heartbeat.
        """
        return self.state in DEGRADED_STATES


class GrantSource(Protocol):
    """What this module needs from a dream store, and nothing more.

    A protocol rather than `DreamStore` itself so the failure path can be
    exercised with something that raises. The real store already catches its own
    `sqlite3.Error` and returns `{}`, which means the outer guard here would be
    untestable against it — and an untested fail-closed path is a fail-closed
    path nobody has watched fail.
    """

    def granted_symbols(self, now: datetime) -> Mapping[str, str]: ...


class AdoptionSource(Protocol):
    """The provenance half. Separate because the two questions are separate."""

    def adoptions(self, dream_id: int | None = None) -> list[Adoption]: ...


class BriefingSource(Protocol):
    """What `brief_grants` needs: the adoption rows and the dreams behind them."""

    def adoptions(self, dream_id: int | None = None) -> list[Adoption]: ...

    def get(self, dream_id: int) -> Dream | None: ...


@dataclass(frozen=True)
class GrantBriefing:
    """What the MODEL is told about symbols an adopted dream permits.

    A third question, kept apart from the other two for the same reason they are
    kept apart from each other. `resolve_grants` answers what may be traded and
    is the only one the gate sees; `resolve_grant_dream_ids` answers which dream
    a trade should be journalled against; this answers what the reasoner needs
    read to it, which is prose and is worth nothing to either of the others.

    **The permission never depends on the narrative.** `symbols` is copied from
    the resolution the gate was handed, so the two cannot disagree, and every
    failure below leaves the symbols standing and drops only the chain. A
    granted symbol the model is told about without its reasoning is a smaller
    loss than a symbol silently missing from the gate's mapping.
    """

    symbols: dict[str, str] = field(default_factory=dict)
    # symbol -> the dream that permits it. Absent for a symbol two live
    # adoptions both claim; see `resolve_grant_dream_ids`.
    dream_ids: dict[str, int] = field(default_factory=dict)
    # The adopted dreams themselves, at most `caps.adopted` of them. The FULL
    # chain reaches the prompt for these and for nothing else, which is the
    # operator's decision and is what bounds the volume.
    dreams: tuple[Dream, ...] = ()
    # dream id -> when its permission lapses. Rendered beside the symbol,
    # because "you may trade this" without "until" is a permission that reads as
    # permanent.
    expires_at: dict[int, datetime] = field(default_factory=dict)

    @property
    def has_grants(self) -> bool:
        return bool(self.symbols)

    @property
    def chains_available(self) -> bool:
        """Whether any reasoning could be read back for these symbols.

        Separate from `has_grants`, and the distinction is the usual one: a
        grant whose dream could not be loaded and a grant whose dream has an
        empty chain both render without a chain, and only the first is a
        degradation. The renderer says which.
        """
        return bool(self.dreams)


def brief_grants(
    store: BriefingSource, resolution: GrantResolution, *, now: datetime
) -> GrantBriefing:
    """Attach the reasoning behind each granted symbol, for the prompt.

    Called once per cycle, only when something is actually granted, so an
    ordinary cycle pays nothing for it.

    **Failing here costs the chain and never the permission.** The symbols are
    taken from `resolution`, which the gate has already been handed, so a store
    error leaves the model told which symbols it may trade and not why. That is
    the right direction: the alternative — dropping a symbol from the prompt
    because its dream would not load — would leave the gate permitting something
    the model was never told about, which is the inert state this whole feature
    exists to leave behind.
    """
    if not resolution.symbols:
        return GrantBriefing()

    symbols = dict(resolution.symbols)
    try:
        dream_ids = resolve_grant_dream_ids(store, symbols, now=now)
        expires: dict[int, datetime] = {}
        for adoption in store.adoptions():
            if not adoption.is_live(now) or adoption.expires_at is None:
                continue
            # The soonest expiry wins where a dream somehow carries two live
            # adoptions. Understating how long a permission lasts is the safe
            # direction; overstating it invites the model to plan around a
            # window that has already shut.
            held = expires.get(adoption.dream_id)
            if held is None or adoption.expires_at < held:
                expires[adoption.dream_id] = adoption.expires_at

        dreams: list[Dream] = []
        for dream_id in sorted(set(dream_ids.values())):
            dream = store.get(dream_id)
            if dream is not None:
                dreams.append(dream)
    except Exception as exc:
        log.warning(
            "grant_briefing_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            detail=(
                "The granted symbols still stand — the gate has them — but the "
                "model is told no reasoning and no expiry for them this cycle."
            ),
        )
        return GrantBriefing(symbols=symbols)

    return GrantBriefing(
        symbols=symbols,
        dream_ids=dream_ids,
        dreams=tuple(dreams),
        expires_at=expires,
    )


def resolve_grants(
    store: GrantSource, rules: Rules, *, now: datetime
) -> GrantResolution:
    """Symbols an adopted dream permits right now, and how much to trust the set.

    The value in the mapping is the `instruments:` key from `config/rules.yaml`
    — "us_equity", "crypto" — and it is the load-bearing half of the pair.
    `Rules.for_symbol` cannot find a granted symbol, because it is in no
    `allowed_symbols` list, so without the class the gate would have no limits
    to apply. A symbol whose class is unknown is a symbol whose risk cap is
    unknown, and it is dropped rather than defaulted.

    Empty is the honest answer to every failure and to every switched-off
    deployment. What `state` adds is which of the five it was, because an empty
    permission set that means "the store would not open" and one that means
    "nothing is adopted" are opposite findings and read identically.
    """
    if not rules.dreaming.allow_symbol_grants:
        return GrantResolution(state=SWITCHED_OFF)
    try:
        return _resolve(store, rules, now)
    except Exception as exc:
        # Broad on purpose, exactly like `fetch_market_ticks`. This runs inside
        # a decision cycle; an unanticipated exception from a store read must
        # cost the permission and never the loop, and the direction it fails in
        # is the safe one.
        log.warning(
            "grant_resolution_failed",
            error=f"{type(exc).__name__}: {exc}",
            detail=(
                "No symbols are granted this cycle. Trading continues on "
                "config/rules.yaml alone."
            ),
        )
        return GrantResolution(state=UNAVAILABLE)


def resolve_granted_symbols(
    store: GrantSource, rules: Rules, *, now: datetime
) -> dict[str, str]:
    """The mapping alone, for a caller with nowhere to report a degradation.

    Every read-only surface — the MCP tools, the vault readout — wants the
    permission and has no heartbeat to put a flag on. The decision loop calls
    `resolve_grants` instead, because that is the one caller whose empty list
    somebody reads later to work out what the bot could see.
    """
    return resolve_grants(store, rules, now=now).symbols


def _resolve(store: GrantSource, rules: Rules, now: datetime) -> GrantResolution:
    live = store.granted_symbols(now)
    enabled = rules.enabled_instruments
    listed = set(rules.allowed_symbols)

    granted: dict[str, str] = {}
    # Sorted so the log line is the same for the same store, which is what makes
    # two cycles comparable by eye.
    for symbol, raw_class in sorted(live.items()):
        class_key = str(raw_class)
        true_key = rules.true_class_key(str(symbol))
        if not true_key or true_key != class_key:
            # **The class hard block, checked against the symbol.** The row's
            # own key is a claim; this is what the symbol actually is, derived
            # from every instrument block's `allowed_symbols` — enabled or not —
            # and from the rule `AlpacaBroker.place_order` routes on. A grant for
            # `BTC/USD` under `us_equity` dies here, and so does one whose class
            # cannot be established at all, because a symbol whose class is
            # unknown is a symbol whose limits are unknown.
            log.warning(
                "grant_dropped_class_mismatch",
                symbol=symbol,
                claimed_class=class_key,
                true_class=true_key or "unknown",
                detail=(
                    "An adopted dream grants this symbol under a class it does "
                    "not belong to. The broker routes on the symbol, not on the "
                    "claim, so honouring this would apply one class's limits to "
                    "an order placed as another's — and a crypto order carries "
                    "no broker-side stop at all."
                ),
            )
            continue
        if class_key not in enabled:
            # The hard block. Not a warning about a typo — it is the rule doing
            # its job, and it is logged so an operator can see a dream asking
            # for something the account has switched off.
            log.warning(
                "grant_dropped_class_not_enabled",
                symbol=symbol,
                asset_class=class_key,
                detail=(
                    "An adopted dream grants this symbol under an instrument "
                    "class that is not enabled in config/rules.yaml. A dream "
                    "may widen the symbols inside an enabled class and can "
                    "never enable a class."
                ),
            )
            continue
        if symbol in listed:
            # Already permitted, so the dream is not what lets this through.
            # Reporting it as granted would put a provenance tag on a trade the
            # allowlist would have permitted anyway.
            log.info(
                "grant_redundant_symbol_already_allowed",
                symbol=symbol,
                asset_class=class_key,
            )
            continue
        granted[symbol] = class_key

    cap = rules.dreaming.max_granted_symbols
    if len(granted) > cap:
        log.error(
            "granted_symbols_over_cap",
            count=len(granted),
            cap=cap,
            symbols=sorted(granted),
            detail=(
                "More symbols are granted than dreaming.max_granted_symbols "
                "allows, so NONE are granted this cycle. Taking an arbitrary "
                "subset would be a permission set nobody can predict. Return "
                "or expire some adoptions, or raise the cap deliberately."
            ),
        )
        return GrantResolution(state=OVER_CAP)
    return GrantResolution(
        symbols=granted, state=GRANTED if granted else NONE_LIVE
    )


def resolve_grant_dream_ids(
    store: AdoptionSource, granted: Mapping[str, str], *, now: datetime
) -> dict[str, int]:
    """Which dream permitted each granted symbol, for the journal to record.

    Provenance, never permission: nothing here decides whether a trade may
    happen, and a missing id costs a `dream_id` on a journal row rather than a
    refusal. So the failure direction is the same as everywhere else — an empty
    mapping — and its consequence is smaller.

    **A symbol two live adoptions both claim comes back with no id at all.**
    There is no correct answer to which dream it came from, and writing one of
    them onto the trade would put a plausible wrong provenance in the journal,
    which is the failure this repository exists to refuse. `granted_symbols`
    already drops a symbol whose two grants disagree about the CLASS; this is
    the same rule for the case where they agree about the class and are still
    two different dreams.
    """
    if not granted:
        return {}
    # **The whole read is inside the guard, not just the query.** It used to
    # wrap `store.adoptions()` alone, so the `is_live` loop below ran outside
    # it — and `is_live` compares datetimes, which raises `TypeError` on a naive
    # `now`. That is the exact shape `claude.propose` was wrapped for: an
    # exception out of a helper, into the decision cycle, killing the loop that
    # reconciles the journal and watches open stops. A guard that covers the
    # network call and not the arithmetic after it guards the half that was
    # already being careful.
    try:
        ids: dict[str, int] = {}
        ambiguous: set[str] = set()
        for adoption in store.adoptions():
            if not adoption.is_live(now):
                continue
            for symbol in adoption.symbols_granted:
                if symbol not in granted:
                    continue
                held = ids.get(symbol)
                if held is not None and held != adoption.dream_id:
                    ambiguous.add(symbol)
                    continue
                ids[symbol] = adoption.dream_id
    except Exception as exc:
        log.warning(
            "grant_provenance_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            detail="Trades opened under a grant this cycle will carry no dream id.",
        )
        return {}

    for symbol in sorted(ambiguous):
        log.warning(
            "grant_provenance_ambiguous",
            symbol=symbol,
            detail=(
                "Two live adoptions grant this symbol, so which dream a trade "
                "in it came from cannot be established. The symbol stays "
                "granted; the trade is journalled with no dream id rather than "
                "with a guess."
            ),
        )
        ids.pop(symbol, None)
    return ids
