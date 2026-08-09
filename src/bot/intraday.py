"""Intraday figures, computed in Python, so a break can be told from a wick.

## The one question this answers

`trend_break` turns entirely on a single distinction: price *closed* through a
level, or it *poked* through and closed back inside. On a daily bar those two
are the same row. That is why the strategy shipped with a `requires` telling
the model to propose nothing on it, and why the strategy was dead weight.

Five-minute bars separate them, and this module does the separating — in code,
against a named level, with a count. The model is handed "closed beyond the
prior session high on 3 of the last 4 bars, on 2.1x average volume" rather than
a series to interpret. Same rule as `indicators.py`: the arithmetic happens
here so that a number nobody can check never reaches a proposal.

## The failed break gets its own field

`strategy.TREND_BREAK` calls the failed break the most common way to lose money
on the idea: a level gives way, everyone piles in, and price closes back inside
within the hour. That is not a subtlety to leave the model to notice in a list
of bars — it is `reclaimed`, it is computed, and it renders as a warning.

## What it deliberately does not claim

**Volume here is an IEX sample, not the market's volume.** Alpaca's free feed
carries a fraction of consolidated tape, so an absolute figure would be
misleading. Every volume statement is therefore a *ratio* against this same
feed's recent average, which is a fair comparison because both sides come from
the same partial sample.

**A session is a UTC date.** US equities trade 14:00-21:00 UTC and never cross
midnight, so grouping five-minute bars by date gives exactly the right answer
for them, and the conventional one for crypto. It would be wrong for a market
whose session spans midnight UTC — CME Globex, if a second broker ever arrives
— and that is called out here rather than discovered later.

**Nothing here is a spread history.** `news_reaction` needs one to judge
"normalised" against, and bars do not carry a spread. Its `requires` keeps
saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Bar

# How many of the most recent bars are examined for an interaction with a level.
# Twelve five-minute bars is an hour, which is the horizon TREND_BREAK's notes
# use when describing a failed break ("closes back inside within the hour").
INTERACTION_BARS = 12

# Bars used for the volume baseline a break is measured against. A session is 78
# five-minute bars, so this is most of one.
VOLUME_BASELINE_BARS = 60


@dataclass(frozen=True)
class LevelTest:
    """What price did at one named level, over the recent bars."""

    name: str
    level: float
    closes_beyond: int
    wicks_only: int
    last_close_beyond: bool
    reclaimed: bool
    volume_ratio: float | None = None

    @property
    def is_clean_break(self) -> bool:
        """Closed through, still through, and not merely one bar's worth."""
        return self.closes_beyond >= 2 and self.last_close_beyond and not self.reclaimed

    def render(self) -> str:
        if self.closes_beyond == 0 and self.wicks_only == 0:
            return f"{self.name} {self.level:,.4f}: untested in the last hour"

        parts = [f"{self.name} {self.level:,.4f}:"]
        parts.append(
            f"{self.closes_beyond} bar(s) CLOSED beyond, "
            f"{self.wicks_only} wicked through without closing beyond"
        )
        if self.volume_ratio is not None:
            parts.append(f"break bar volume {self.volume_ratio:.2f}x the recent average")
        if self.reclaimed:
            parts.append(
                "RECLAIMED — it closed beyond and has since closed back inside, "
                "which is a failed break, not an entry"
            )
        elif self.last_close_beyond:
            parts.append("still beyond as of the last bar")
        else:
            parts.append("not beyond as of the last bar")
        return " ".join(parts)


@dataclass(frozen=True)
class IntradayView:
    """The intraday picture for one symbol. `None` fields mean not enough bars."""

    symbol: str
    bar_count: int
    minutes: int
    last_close: float
    session_high: float | None = None
    session_low: float | None = None
    prior_session_high: float | None = None
    prior_session_low: float | None = None
    avg_bar_volume: float | None = None
    tests: list[LevelTest] = field(default_factory=list)

    @property
    def has_levels(self) -> bool:
        """Whether a prior session was available to draw a level from."""
        return self.prior_session_high is not None or self.prior_session_low is not None


def _by_session(bars: list[Bar]) -> dict[date, list[Bar]]:
    grouped: dict[date, list[Bar]] = {}
    for bar in bars:
        grouped.setdefault(bar.timestamp.date(), []).append(bar)
    return grouped


def _test_level(
    name: str, level: float, recent: list[Bar], above: bool, baseline: float | None
) -> LevelTest:
    """Classify each recent bar's interaction with one level.

    `above` says which side counts as beyond: True for a resistance level being
    broken upward, False for support being broken downward. Two separate counts
    come out of it — bars that CLOSED beyond, and bars that only traded beyond
    and closed back — because that difference is the entire strategy.
    """
    closes_beyond = 0
    wicks_only = 0
    break_volume: float | None = None
    seen_close_beyond = False

    for bar in recent:
        closed = bar.close > level if above else bar.close < level
        touched = bar.high > level if above else bar.low < level
        if closed:
            closes_beyond += 1
            if break_volume is None:
                break_volume = bar.volume
            seen_close_beyond = True
        elif touched:
            wicks_only += 1

    last = recent[-1]
    last_close_beyond = last.close > level if above else last.close < level

    return LevelTest(
        name=name,
        level=level,
        closes_beyond=closes_beyond,
        wicks_only=wicks_only,
        last_close_beyond=last_close_beyond,
        # Beyond at some point, back inside now. The failed break.
        reclaimed=seen_close_beyond and not last_close_beyond,
        volume_ratio=(
            break_volume / baseline if break_volume is not None and baseline else None
        ),
    )


def compute(symbol: str, bars: list[Bar], minutes: int) -> IntradayView | None:
    """The intraday view for one symbol. `None` when there are no bars at all.

    Bars are expected oldest first, as every Alpaca bars endpoint returns them.
    A short history yields a view with `None` levels and no tests rather than an
    invented one, exactly as `indicators.compute` does.
    """
    if not bars:
        return None

    sessions = _by_session(bars)
    days = sorted(sessions)
    current = sessions[days[-1]]
    prior = sessions[days[-2]] if len(days) >= 2 else []

    baseline_bars = bars[-VOLUME_BASELINE_BARS:]
    baseline = (
        sum(b.volume for b in baseline_bars) / len(baseline_bars)
        if baseline_bars
        else None
    )

    prior_high = max((b.high for b in prior), default=None) if prior else None
    prior_low = min((b.low for b in prior), default=None) if prior else None

    recent = bars[-INTERACTION_BARS:]
    tests: list[LevelTest] = []
    if prior_high is not None:
        tests.append(
            _test_level("prior session high", prior_high, recent, True, baseline)
        )
    if prior_low is not None:
        tests.append(
            _test_level("prior session low", prior_low, recent, False, baseline)
        )

    return IntradayView(
        symbol=symbol,
        bar_count=len(bars),
        minutes=minutes,
        last_close=bars[-1].close,
        session_high=max(b.high for b in current),
        session_low=min(b.low for b in current),
        prior_session_high=prior_high,
        prior_session_low=prior_low,
        avg_bar_volume=baseline,
        tests=tests,
    )


def render(view: IntradayView) -> list[str]:
    """Lines for the market context block. Computed values only, never bars."""
    lines = [
        f"- {view.symbol}: {view.minutes}-minute bars, {view.bar_count} of them, "
        f"last close {view.last_close:,.4f}"
    ]

    if view.session_high is not None and view.session_low is not None:
        lines.append(
            f"    session so far: high {view.session_high:,.4f}, "
            f"low {view.session_low:,.4f}"
        )

    if not view.has_levels:
        # Named rather than omitted, for the same reason a missing sma_200 is
        # printed as unavailable: a symbol that silently carries no levels looks
        # identical to one whose levels were simply never tested.
        lines.append(
            "    NO PRIOR SESSION in the intraday history, so there is no "
            "prior-session high or low to break. Do not infer one."
        )
        return lines

    for test in view.tests:
        lines.append(f"    {test.render()}")
    return lines


def summarise(view: IntradayView) -> str:
    """One line for the audit record, so a decision can be replayed later."""
    if not view.tests:
        return f"{view.minutes}m bars: no prior-session level available"
    return f"{view.minutes}m bars: " + "; ".join(
        f"{t.name} {'clean break' if t.is_clean_break else 'no clean break'}"
        + (" (reclaimed)" if t.reclaimed else "")
        for t in view.tests
    )
