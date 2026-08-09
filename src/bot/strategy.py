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

`requires` is the list of things a strategy needs that the market context does
not carry. It renders into the prompt as an explicit "you do not have this,
do not estimate it, propose nothing", and it exists because a model asked to
apply a moving-average filter it cannot see will not decline — it will estimate
one, phrase the estimate confidently, and the risk gate will wave it through,
because the gate checks size and stops rather than whether the reasoning was
invented. That is the Alpha Arena failure mode arriving through the data layer.

**Two of the four are now evaluable.** `Broker.get_daily_bars` supplies daily
history, `src/bot/indicators.py` computes the averages, the ATR, the volume
average and the swing levels **in Python**, and `context.py` renders the answers.
So mean reversion and momentum carry no `requires` any more: everything they
name is measured and handed over.

**Two are still not.** Trend break and news reaction both need intraday bars,
and news reaction also needs a spread history to compare against. Neither is
fetched, so both keep a `requires` naming exactly what is still absent. Trimming
those lists to nothing because the file now has *some* history would be the
worst outcome available: the warning would disappear while the gap stayed.

The remaining work is `get_intraday_bars` on the `Broker` protocol, which Alpaca
also supplies free, and a spread history to make "the spread has normalised"
checkable.
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
                + ". The market context carries daily bars and the indicators "
                "computed from them, and nothing finer. Do not estimate the values "
                "listed above, do not reason as though you can see them, and do not "
                "propose a trade whose justification depends on them. Say that the "
                "data is missing and propose nothing."
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
    notes=(
        "The trend filter is the load-bearing part and the one people drop first. "
        "Without it this is a strategy for buying things on their way to zero: the "
        "cheapest-looking entry is a company in genuine trouble. "
        "Every condition above is measured for you in the Indicators section of the "
        "market context: the two averages, the ATR, the distance from the 20-day in "
        "ATR, the volume against its average, and the most recent confirmed swing "
        "low. Read those figures. Do not recompute them, and if one of them is "
        "reported as unavailable for a symbol, that condition cannot be checked and "
        "the trade is not there to take."
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
    notes=(
        "This is now evaluable, and the figures are supplied rather than left to "
        "you. The Indicators section carries daily bars, volume against its "
        "average, the most recent confirmed swing high and low, and the 60-session "
        "close range. The Intraday section carries the part that used to be "
        "missing: for the prior session's high and low it states how many "
        "five-minute bars CLOSED beyond the level, how many only wicked through, "
        "the volume on the breaking bar as a multiple of its recent average, and "
        "whether the level has been RECLAIMED. "
        "Use those counts. Do not read 'wicked through' as a break, and do not "
        "call a break on a symbol whose intraday bars are reported as "
        "unavailable — for that symbol the distinction genuinely cannot be made. "
        "The failed break is the most common way to lose money on this: a level "
        "gives way, everyone piles in, and price closes back inside within the "
        "hour. That case is computed for you and labelled RECLAIMED, and it is a "
        "reason to stand aside rather than a cheaper entry. The invalidation "
        "above is deliberately tight for the same reason. "
        "SPY is the vehicle for an S&P 500 view here. Alpaca does not offer "
        "futures, so ES and MES are unavailable without a second broker — see "
        "docs/HANDOFF.md."
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
        "a spread history to judge 'normalised' against — the market context "
        "carries the current spread and nothing to compare it to",
    ],
    notes=(
        "PARTLY evaluable, and the missing piece is named above rather than "
        "waved at. The earnings calendar IS available, through Finnhub, and it "
        "is what the news blackout gate reads. Intraday bars ARE now supplied, "
        "so the initial spike and the direction that survived it are no longer "
        "the same number. What is still absent is a spread history: the context "
        "gives the current spread on one line and nothing to say whether that is "
        "wide or ordinary for this symbol, so the entry condition 'the spread "
        "has normalised' cannot be checked. Do not substitute a guess at what "
        "normal looks like. "
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
    notes=(
        "The 60-session close range, the volume ratio and the most recent confirmed "
        "swing low are all in the Indicators section, so the new high, the "
        "participation and the invalidation level are measured rather than guessed. "
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
