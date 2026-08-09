"""Indicators are asserted against hand-computed answers, never against themselves.

The point of computing these in Python rather than asking the model is that the
answers can be checked. A test that recomputed an average with the same code it
is testing would prove nothing, so every expected value here is worked out
independently and written down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.indicators import (
    ATR_PERIOD,
    Indicators,
    average_true_range,
    compute,
    render,
    simple_moving_average,
    swing_levels,
)
from bot.models import Bar

START = datetime(2026, 1, 5, tzinfo=UTC)


def bar(index: int, *, high: float, low: float, close: float, volume: float = 1000.0) -> Bar:
    return Bar(
        symbol="TEST",
        timestamp=START + timedelta(days=index),
        open=(high + low) / 2,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def flat_bars(closes: list[float], *, spread: float = 1.0) -> list[Bar]:
    return [
        bar(i, high=c + spread, low=c - spread, close=c) for i, c in enumerate(closes)
    ]


# ------------------------------------------------------------------ averages


def test_simple_moving_average_uses_only_the_last_n():
    # Mean of the last three of [1..10] is (8 + 9 + 10) / 3 = 9.
    assert simple_moving_average([float(n) for n in range(1, 11)], 3) == 9.0


def test_simple_moving_average_is_none_when_there_are_too_few():
    assert simple_moving_average([1.0, 2.0], 3) is None


def test_short_history_reports_the_long_average_as_missing_rather_than_guessing():
    """A 200-day average over 40 bars is a 40-day average wearing the wrong label."""
    result = compute("TEST", flat_bars([100.0 + i for i in range(40)]))

    assert result is not None
    assert result.sma_20 is not None
    assert result.sma_200 is None
    assert "200-day average" in result.missing
    assert result.above_sma_200 is None


# ----------------------------------------------------------------------- ATR


def test_average_true_range_matches_a_hand_computed_wilder_average():
    """Every true range is 4.0, so any smoothing of them is also 4.0."""
    bars = [bar(i, high=102.0, low=98.0, close=100.0) for i in range(ATR_PERIOD + 1)]
    assert average_true_range(bars) == 4.0


def test_average_true_range_counts_a_gap_against_the_previous_close():
    """True range is the widest of the three measures, not just high minus low."""
    bars = [bar(0, high=101.0, low=99.0, close=100.0)]
    # Gaps up to a 2-point range whose low is 8 above the previous close: the
    # bar's own range is 2, but high minus previous close is 12.
    bars.append(bar(1, high=112.0, low=110.0, close=111.0))

    from bot.indicators import true_ranges

    assert true_ranges(bars)[1] == 12.0


def test_average_true_range_needs_one_more_bar_than_its_period():
    """The first true range is discarded, having no previous close to measure from."""
    bars = [bar(i, high=102.0, low=98.0, close=100.0) for i in range(ATR_PERIOD)]
    assert average_true_range(bars) is None
    assert average_true_range([*bars, bar(99, high=102.0, low=98.0, close=100.0)]) == 4.0


# -------------------------------------------------------------------- swings


def test_swing_levels_finds_the_most_recent_confirmed_pivot():
    # A clear peak at index 3 and a clear trough at index 7, both with two
    # lower/higher bars either side.
    closes = [10.0, 11.0, 12.0, 15.0, 12.0, 11.0, 10.0, 7.0, 10.0, 11.0, 12.0]
    high, low = swing_levels(flat_bars(closes))

    assert high == 16.0  # 15.0 close, +1.0 spread
    assert low == 6.0  # 7.0 close, -1.0 spread


def test_swing_levels_ignores_pivots_the_bars_have_not_confirmed_yet():
    """The last two bars can never qualify: what would confirm them has not happened.

    A level that may not survive the next session is precisely the level not to
    hang a stop on, so an unconfirmed pivot is withheld rather than reported.
    """
    # The highest bar is the very last one, which cannot be a confirmed swing.
    closes = [10.0, 11.0, 12.0, 15.0, 12.0, 11.0, 10.0, 9.0, 10.0, 11.0, 40.0]
    high, _ = swing_levels(flat_bars(closes))

    assert high == 16.0
    assert high != 41.0


def test_swing_levels_are_none_without_enough_bars():
    assert swing_levels(flat_bars([1.0, 2.0, 3.0])) == (None, None)


# ------------------------------------------------------------------- derived


def test_distance_from_the_average_is_signed_and_measured_in_atr():
    indicators = Indicators(
        symbol="TEST", bar_count=250, last_close=90.0, last_volume=1000.0,
        sma_20=100.0, atr_14=5.0,
    )
    # 10 below a 100 average, with a 5-point ATR, is two true ranges below.
    assert indicators.distance_from_sma_20_atr == -2.0


def test_distance_from_the_average_is_none_when_either_input_is_missing():
    assert (
        Indicators(
            symbol="TEST", bar_count=5, last_close=90.0, last_volume=1.0, sma_20=100.0
        ).distance_from_sma_20_atr
        is None
    )


def test_volume_ratio_reports_participation_against_the_average():
    indicators = Indicators(
        symbol="TEST", bar_count=250, last_close=1.0, last_volume=1500.0,
        avg_volume_20=1000.0,
    )
    assert indicators.volume_ratio == 1.5


def test_trend_filter_distinguishes_below_from_unknown():
    below = Indicators(
        symbol="TEST", bar_count=250, last_close=90.0, last_volume=1.0, sma_200=100.0
    )
    unknown = Indicators(symbol="TEST", bar_count=5, last_close=90.0, last_volume=1.0)

    assert below.above_sma_200 is False
    assert unknown.above_sma_200 is None


# -------------------------------------------------------------------- compute


def test_compute_returns_none_for_no_bars():
    assert compute("TEST", []) is None


def test_compute_fills_everything_given_a_full_history():
    result = compute("TEST", flat_bars([100.0 + (i % 7) for i in range(250)]))

    assert result is not None
    assert result.bar_count == 250
    assert result.sma_20 is not None
    assert result.sma_200 is not None
    assert result.atr_14 is not None
    assert result.avg_volume_20 == 1000.0
    assert result.missing == []


def test_compute_caps_the_breakout_lookback_at_the_bars_available():
    result = compute("TEST", flat_bars([100.0 + i for i in range(30)]))

    assert result is not None
    assert result.lookback_used == 30
    assert result.highest_close == 129.0
    assert result.at_lookback_high is True


# --------------------------------------------------------------------- render


def test_render_names_every_missing_figure_rather_than_omitting_it():
    """Silence would read as "there is nothing to report", which is the wrong claim."""
    result = compute("TEST", flat_bars([100.0, 101.0, 102.0]))
    assert result is not None

    text = "\n".join(render(result))

    assert "200-day average: unavailable" in text
    assert f"ATR({ATR_PERIOD}): unavailable" in text
    assert "NOT AVAILABLE for this symbol" in text
    assert "Do not estimate these" in text


def test_render_shouts_when_price_is_below_the_trend_filter():
    """The one condition whose failure kills the mean-reversion trade outright."""
    bars = flat_bars([200.0 - i * 0.5 for i in range(250)])
    result = compute("TEST", bars)
    assert result is not None

    text = "\n".join(render(result))

    assert result.above_sma_200 is False
    assert "PRICE IS BELOW IT" in text
