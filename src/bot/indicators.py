"""Indicators computed in Python, from bars, before the model sees anything.

Pure functions over `list[Bar]`, deliberately independent of the broker, so
every figure can be asserted against a hand-computed answer.

## Why this file is computed rather than asked

The strategies in `src/bot/strategy.py` are written against a 20-day average, a
200-day average, an ATR and a volume average. Until this module existed the
market context carried one live quote per symbol and no history at all, so none
of them was evaluable, and every strategy shipped a `requires` list telling the
model to propose nothing.

The failure that list existed to prevent is specific and worth restating. A
model asked to apply a 200-day moving-average filter it cannot see does not
decline. It estimates one, states the estimate confidently, and the risk gate
approves the trade, because the gate checks size and stops rather than whether
the reasoning was invented. Handing the model bars and asking it to do the
arithmetic reintroduces exactly that: a number nobody can check, arrived at by
a process with no error bars.

So the arithmetic happens here, in code, and the model is handed the answers.

## Missing is missing

Every field is `None` when there are not enough bars to compute it honestly.
`sma_200` over 40 bars is not an approximate 200-day average, it is a 40-day
average wearing the wrong label, and it is worse than nothing because it will be
believed. `render` prints those as unavailable and names them, following the
same rule as `reconcile`'s `risk_is_understated` and `FinnhubCalendar.is_degraded`:
say the number is unknown rather than imply it is zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Bar

# Periods. These are the ones config/rules.yaml and strategy.py actually name;
# adding more would be inventing questions nobody asked.
SMA_FAST = 20
SMA_SLOW = 200
ATR_PERIOD = 14
VOLUME_PERIOD = 20

# A swing is confirmed when it stands as the extreme of the bars either side of
# it. Two is the smallest window that is not simply "yesterday was lower", and
# it costs two bars of lag, which is the price of the level being real.
SWING_WINDOW = 2

# Lookback for "a new high over a meaningful lookback" in the momentum strategy.
BREAKOUT_LOOKBACK = 60


@dataclass(frozen=True)
class Indicators:
    """Everything the strategies name, or `None` where the bars cannot supply it."""

    symbol: str
    bar_count: int
    last_close: float
    last_volume: float

    sma_20: float | None = None
    sma_200: float | None = None
    atr_14: float | None = None
    avg_volume_20: float | None = None

    # Most recent confirmed pivots. The invalidation levels the strategies
    # describe ("a close below the swing low that produced the signal") are
    # these, not a round number.
    swing_high: float | None = None
    swing_low: float | None = None

    # Range boundaries over the breakout lookback, which is what "a level that
    # has repeatedly held" and "a new high" are measured against.
    highest_close: float | None = None
    lowest_close: float | None = None
    lookback_used: int = 0

    @property
    def distance_from_sma_20_atr(self) -> float | None:
        """How far price sits from its 20-day average, in true ranges.

        Negative means below. This is the units the mean-reversion entry is
        written in ("stretched below its 20-day average by more than one ATR"),
        and expressing it in ATR rather than per cent is what makes one
        threshold work across a $70 stock and a $700 one.
        """
        if self.sma_20 is None or self.atr_14 is None or self.atr_14 <= 0:
            return None
        return (self.last_close - self.sma_20) / self.atr_14

    @property
    def above_sma_200(self) -> bool | None:
        """The trend filter. `None` means unknown, which is not the same as False."""
        if self.sma_200 is None:
            return None
        return self.last_close > self.sma_200

    @property
    def volume_ratio(self) -> float | None:
        """Today's volume against its 20-day average. Participation, in one number."""
        if self.avg_volume_20 is None or self.avg_volume_20 <= 0:
            return None
        return self.last_volume / self.avg_volume_20

    @property
    def at_lookback_high(self) -> bool | None:
        if self.highest_close is None:
            return None
        return self.last_close >= self.highest_close

    @property
    def missing(self) -> list[str]:
        """Names of the fields the bars could not support. Rendered, not hidden."""
        absent: list[str] = []
        if self.sma_20 is None:
            absent.append(f"{SMA_FAST}-day average")
        if self.sma_200 is None:
            absent.append(f"{SMA_SLOW}-day average")
        if self.atr_14 is None:
            absent.append(f"ATR({ATR_PERIOD})")
        if self.avg_volume_20 is None:
            absent.append(f"{VOLUME_PERIOD}-day average volume")
        if self.swing_low is None and self.swing_high is None:
            absent.append("swing levels")
        return absent


def simple_moving_average(values: list[float], period: int) -> float | None:
    """Mean of the last `period` values, or `None` if there are not that many."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def true_ranges(bars: list[Bar]) -> list[float]:
    """True range per bar. The first has no previous close, so it is the bar's range."""
    ranges: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[i - 1].close
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return ranges


def average_true_range(bars: list[Bar], period: int = ATR_PERIOD) -> float | None:
    """Wilder's ATR.

    Wilder's smoothing rather than a simple mean of true ranges, because that is
    what "ATR" means everywhere else and a stop placed at two of the wrong ATR is
    a different stop. It needs `period + 1` bars: the first true range is
    discarded, having had no previous close to measure against.
    """
    if period <= 0 or len(bars) < period + 1:
        return None

    ranges = true_ranges(bars)[1:]
    atr = sum(ranges[:period]) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def swing_levels(
    bars: list[Bar], window: int = SWING_WINDOW
) -> tuple[float | None, float | None]:
    """Most recent confirmed swing high and swing low.

    A bar is a swing high when its high is the highest of the `window` bars on
    each side, and vice versa. The last `window` bars can never qualify, because
    the bars that would confirm them have not happened yet. Returning an
    unconfirmed pivot would hand the model a level that may not survive the
    next session, which is precisely the level not to hang a stop on.
    """
    if window < 1 or len(bars) < window * 2 + 1:
        return None, None

    high: float | None = None
    low: float | None = None

    for i in range(len(bars) - window - 1, window - 1, -1):
        neighbours = bars[i - window : i] + bars[i + 1 : i + window + 1]
        if high is None and all(bars[i].high > b.high for b in neighbours):
            high = bars[i].high
        if low is None and all(bars[i].low < b.low for b in neighbours):
            low = bars[i].low
        if high is not None and low is not None:
            break

    return high, low


def compute(symbol: str, bars: list[Bar]) -> Indicators | None:
    """Everything the strategies name, from daily bars. `None` if there are none.

    Bars are expected oldest first, which is the order every Alpaca bars
    endpoint returns them in.
    """
    if not bars:
        return None

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    swing_high, swing_low = swing_levels(bars)

    lookback = min(BREAKOUT_LOOKBACK, len(closes))
    window = closes[-lookback:]

    return Indicators(
        symbol=symbol,
        bar_count=len(bars),
        last_close=closes[-1],
        last_volume=volumes[-1],
        sma_20=simple_moving_average(closes, SMA_FAST),
        sma_200=simple_moving_average(closes, SMA_SLOW),
        atr_14=average_true_range(bars),
        avg_volume_20=simple_moving_average(volumes, VOLUME_PERIOD),
        swing_high=swing_high,
        swing_low=swing_low,
        highest_close=max(window),
        lowest_close=min(window),
        lookback_used=lookback,
    )


def render(indicators: Indicators) -> list[str]:
    """Lines for the market context block. One symbol, computed values only."""
    lines = [f"- {indicators.symbol}: close {indicators.last_close:,.4f}"]

    def figure(label: str, value: float | None, fmt: str = ",.4f") -> None:
        if value is None:
            lines.append(f"    {label}: unavailable")
        else:
            lines.append(f"    {label}: {value:{fmt}}")

    figure(f"{SMA_FAST}-day average", indicators.sma_20)
    figure(f"{SMA_SLOW}-day average", indicators.sma_200)
    figure(f"ATR({ATR_PERIOD})", indicators.atr_14)

    distance = indicators.distance_from_sma_20_atr
    if distance is None:
        lines.append(f"    distance from the {SMA_FAST}-day average: unavailable")
    else:
        side = "above" if distance >= 0 else "below"
        lines.append(
            f"    distance from the {SMA_FAST}-day average: "
            f"{abs(distance):.2f} ATR {side}"
        )

    trend = indicators.above_sma_200
    if trend is None:
        lines.append(f"    {SMA_SLOW}-day trend filter: unavailable")
    else:
        lines.append(
            f"    {SMA_SLOW}-day trend filter: "
            f"{'price is above it' if trend else 'PRICE IS BELOW IT'}"
        )

    ratio = indicators.volume_ratio
    if ratio is None:
        lines.append("    volume against its average: unavailable")
    else:
        lines.append(
            f"    volume against its average: {ratio:.2f}x "
            f"({indicators.last_volume:,.0f} against {indicators.avg_volume_20:,.0f})"
        )

    figure("most recent confirmed swing high", indicators.swing_high)
    figure("most recent confirmed swing low", indicators.swing_low)

    if indicators.highest_close is not None and indicators.lowest_close is not None:
        lines.append(
            f"    {indicators.lookback_used}-session close range: "
            f"{indicators.lowest_close:,.4f} to {indicators.highest_close:,.4f}"
        )

    lines.append(f"    bars available: {indicators.bar_count}")

    absent = indicators.missing
    if absent:
        lines.append(
            "    NOT AVAILABLE for this symbol: "
            + ", ".join(absent)
            + ". Do not estimate these, and do not propose a trade whose "
            "justification depends on them."
        )

    return lines


def summarise(indicators: Indicators) -> str:
    """One line per symbol, for the audit record.

    The full `render` output is what the model reads; this is what gets stored
    next to the decision so a past cycle can be re-read months later. It keeps
    the figures that decide a mean-reversion entry and drops the rest, because
    an audit log that stores everything is one nobody opens.

    Unavailable stays unavailable here too. A stored "sma_200=0" would be read
    back as a real reading by whoever looks at this next.
    """

    def figure(value: float | None, fmt: str = ",.2f") -> str:
        return "unavailable" if value is None else f"{value:{fmt}}"

    distance = indicators.distance_from_sma_20_atr
    ratio = indicators.volume_ratio
    trend = indicators.above_sma_200

    return (
        f"close {indicators.last_close:,.2f}, "
        f"sma{SMA_FAST} {figure(indicators.sma_20)}, "
        f"sma{SMA_SLOW} {figure(indicators.sma_200)}, "
        f"atr {figure(indicators.atr_14)}, "
        f"{'unavailable' if distance is None else f'{distance:+.2f} ATR from sma{SMA_FAST}'}, "
        f"volume {'unavailable' if ratio is None else f'{ratio:.2f}x'}, "
        f"trend {'unavailable' if trend is None else ('above' if trend else 'BELOW')}"
    )
