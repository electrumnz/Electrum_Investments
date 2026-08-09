"""Base strategies. Placeholders with a shape, not an edge.

`docs/HANDOFF.md` says the strategy is the genuinely hard part and belongs to the
operator. That is still true. What this module supplies is the *scaffolding*: a
thesis stated so it can be wrong, entry conditions written as observations rather
than intentions, and an invalidation level that becomes the stop. "Buy oversold
large-caps in an uptrend" is testable. "Trade well" is not.

## The honest part

**None of these has a demonstrated edge.** They are conventional, they are
defensible, and they are unproven here. Treat any of them as a hypothesis to
measure with `src/bot/metrics.py`, not as a reason to trust a proposal.

## The part that matters most

**The bot cannot currently evaluate any of these.** `Broker.get_tick` returns a
single bid/ask, so the market context carries one live quote per symbol and no
history at all: no bars, no moving average, no RSI, no ATR, no trendline.

That gap is the whole reason `requires` exists on every strategy below and gets
rendered into the prompt. A model asked to apply a moving-average filter it
cannot see will not decline — it will estimate one, phrase the estimate
confidently, and the risk gate will wave it through because the gate checks size
and stops rather than whether the reasoning was invented. That is the Alpha
Arena failure mode arriving through the data layer.

So until the context carries real history, these read as "here is the shape of
the idea, and here is what you do not have" — and the correct output remains
nothing.

**Closing that gap is the highest-value next task in this repo.** It needs
historical bars on the `Broker` protocol (Alpaca supplies them free), the
indicators computed in Python rather than by the model, and both rendered into
`context.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Strategy:
    """One tradeable idea, written so that it can be shown to be wrong."""

    name: str
    thesis: str
    entry: list[str]
    invalidation: str
    exit: str
    requires: list[str] = field(default_factory=list)
    notes: str = ""

    def render(self) -> str:
        lines = [f"Thesis: {self.thesis}", "Enter only when all of these hold:"]
        lines += [f"  - {c}" for c in self.entry]
        lines.append(f"Invalidation (this is your stop): {self.invalidation}")
        lines.append(f"Exit: {self.exit}")
        if self.notes:
            lines.append(f"Note: {self.notes}")
        if self.requires:
            lines.append(
                "DATA YOU DO NOT HAVE: "
                + "; ".join(self.requires)
                + ". The market context carries one current quote per symbol and no "
                "history. Do not estimate these values, do not reason as though you "
                "can see them, and do not propose a trade whose justification depends "
                "on them. Say that the data is missing and propose nothing."
            )
        return "\n".join(lines)


MEAN_REVERSION = Strategy(
    name="mean_reversion",
    thesis=(
        "A liquid large-cap that has moved sharply away from its recent average, "
        "while its longer-term trend is still up, tends to revert toward that "
        "average rather than continue"
    ),
    entry=[
        "the instrument is above its 200-day moving average, so the longer trend is up",
        "price is stretched below its 20-day average by more than one ATR",
        "there is evidence of exhaustion rather than acceleration: a slowing range, "
        "a failed new low, or volume tapering into the low",
        "no earnings announcement inside the blackout window",
    ],
    invalidation=(
        "a close below the swing low that produced the signal, or roughly 2 ATR "
        "below entry, whichever is nearer"
    ),
    exit="the 20-day average, or roughly 3 ATR, whichever comes first",
    requires=["a 20-day and 200-day moving average", "ATR", "recent swing lows", "volume"],
    notes=(
        "The trend filter is the load-bearing part and the one people drop first. "
        "Without it this is a strategy for buying things on their way to zero: the "
        "cheapest-looking entry is a company in genuine trouble."
    ),
)

TREND_BREAK = Strategy(
    name="trend_break",
    thesis=(
        "A level that has repeatedly held tells you where the market disagrees. "
        "When it finally gives way on real participation, the move that follows "
        "has room, because the traders defending it are now wrong"
    ),
    entry=[
        "a level is identifiable and has been tested at least twice: a trendline, "
        "a range boundary, or a prior session's high or low",
        "price has closed through it rather than merely wicked through",
        "participation confirms the break — volume above its recent average",
        "the break is in the direction of the higher-timeframe trend, or the level "
        "is significant enough to mark a genuine change in it",
    ],
    invalidation=(
        "price reclaims the broken level and closes back inside. That is the whole "
        "idea failing, not a drawdown to sit through"
    ),
    exit=(
        "the next structural level, or a measured move equal to the height of the "
        "range that broke"
    ),
    requires=[
        "intraday and daily bars",
        "volume and its recent average",
        "identified support and resistance levels",
    ],
    notes=(
        "SPY is the vehicle for an S&P 500 view here. Alpaca does not offer futures, "
        "so ES and MES are unavailable without a second broker — see docs/HANDOFF.md. "
        "The failed break is the most common way to lose money on this: a level gives "
        "way, everyone piles in, and price closes back inside within the hour. The "
        "invalidation above is deliberately tight for that reason."
    ),
)

NEWS_REACTION = Strategy(
    name="news_reaction",
    thesis=(
        "The first move after a scheduled announcement is noise and spread. The "
        "direction that survives the first half hour is information"
    ),
    entry=[
        "a scheduled announcement has passed and its blackout window has expired",
        "price has established a direction and held it — not the initial spike",
        "the spread has normalised back toward its usual level",
        "there is a level from the reaction itself to place a stop behind",
    ],
    invalidation="a return through the level established after the announcement",
    exit="a measured move, or the close of the session in which the news landed",
    requires=[
        "an economic and earnings calendar with timestamps",
        "intraday bars covering the announcement",
        "historical spread for comparison",
    ],
    notes=(
        "This works WITH the news blackout in config/rules.yaml, not against it. "
        "The blackout refuses new positions for 15 minutes either side of a "
        "scheduled announcement, which forbids trading INTO the release — the part "
        "that is a coin flip on a widened spread. It does not forbid trading the "
        "reaction once it has established itself, which is the part that carries "
        "information. If the two ever feel like they are fighting, the blackout wins."
    ),
)

MOMENTUM = Strategy(
    name="momentum",
    thesis=(
        "An instrument making new highs on expanding participation tends to keep "
        "doing so for longer than feels comfortable"
    ),
    entry=[
        "a new high over a meaningful lookback, not an intraday tick",
        "participation expanding rather than contracting into the high",
        "no earnings announcement inside the blackout window",
    ],
    invalidation="a close below the last higher low",
    exit="a trailing stop behind successive higher lows",
    requires=["daily bars over the lookback", "volume and its recent average"],
    notes=(
        "Configured for the crypto sleeve, which is currently disabled. Momentum and "
        "mean reversion are opposite bets on the same observation, so running both on "
        "one instrument at once is how a book ends up flat and paying commission."
    ),
)

UNSPECIFIED = Strategy(
    name="unspecified",
    thesis="No strategy has been defined for this instrument class",
    entry=["nothing — there is no defined edge to act on"],
    invalidation="not applicable",
    exit="not applicable",
    notes="Propose nothing. An undefined strategy is not an invitation to improvise one.",
)

REGISTRY: dict[str, Strategy] = {
    s.name: s for s in (MEAN_REVERSION, TREND_BREAK, NEWS_REACTION, MOMENTUM, UNSPECIFIED)
}


def guidance_for(name: str) -> str:
    """Prompt text for a strategy name, falling back loudly rather than silently."""
    strategy = REGISTRY.get(name)
    if strategy is None:
        return (
            f"No strategy named '{name}' is defined in src/bot/strategy.py. Treat this "
            "instrument class as having no edge and propose nothing."
        )
    return strategy.render()
