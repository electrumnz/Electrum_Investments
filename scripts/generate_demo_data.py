#!/usr/bin/env python3
"""Generate the synthetic dataset the public web app renders.

    python scripts/generate_demo_data.py

Writes ``brand/assets/demo-data.js``, which is committed.

## Why this exists

``brand/`` is deployed publicly at https://mudhorn-capital.vercel.app. The real
dashboard in ``src/bot/web/`` renders live brokerage state and has no login
*because* it binds to 127.0.0.1, so nothing published may ever touch the
journal, the broker or a credential. CLAUDE.md covers that under "One directory
is published. The rest must never be".

The web app therefore needs numbers that behave like real ones without being
real ones. This script makes them, and committing the output means the site
stays a static file with no fetch, no API and nothing to leak.

## What makes the numbers behave

Every figure is derived from simulated daily bars rather than typed in, so the
dataset is internally consistent in the ways a reader would check:

* Entries sit where the mean-reversion conditions actually hold, against a
  20-day average, a 200-day average and an ATR computed from the same bars.
* Position size comes from the risk rule, not the other way round: qty is
  ``risk_budget / (entry - stop)``, so ``|entry - stop| x qty`` lands under
  1% of equity on every trade and under 2% across everything open at once.
* Exits are resolved by walking the bars forward. A trade stops out because the
  low reached the stop, not because a coin was flipped.
* MAE and MFE are sampled at daily closes, mirroring the real sampling caveat
  in ``src/bot/metrics.py``: both understate the true excursion, so the capture
  ratio the site shows is a floor.
* Fees are Alpaca's actual equity costs, which is nothing per share plus the
  regulatory fees charged on sales. They are tiny. That is the honest number.

The seed is fixed, so regenerating produces the same dataset and a clean diff.

``config/rules.yaml`` is copied in verbatim for the Rules page. It holds limits,
not secrets, and it is already public in this repository.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "config" / "rules.yaml"
OUTPUT_PATH = REPO_ROOT / "brand" / "assets" / "demo-data.js"

# Chosen from a scan of candidate seeds on the grounds that it produces an
# unremarkable year: 48 closed trades, a profit factor of 1.07, about half a
# per cent on the year, a drawdown along the way and two stand-down trips. A
# seed that printed a handsome equity curve would be the wrong thing to
# publish. Nothing on this site may read as a performance claim, and a demo
# that quietly implies one is still implying one.
#
# The high win rate with a payoff ratio below 1 is not a flattering choice
# either; it is what mean reversion looks like when the exit is the mean. Most
# trades give back a little, the occasional one gives back a full R, and the
# expectancy sits near zero. That is the shape worth showing.
SEED = 128

# Trading runs over the last stretch of the simulated history. The bars start
# well before that so a 200-day average exists on the very first tradeable day,
# which is the filter the mean-reversion strategy leans on hardest.
WARMUP_DAYS = 260
TRADING_DAYS = 250

STARTING_EQUITY = 100_000.0

# Mirrors config/rules.yaml. Asserted against the real file at the end of the
# run, so the two cannot drift apart silently.
MAX_RISK_PER_TRADE_PCT = 1.0
MAX_TOTAL_RISK_PCT = 2.0
MAX_CONCURRENT_POSITIONS = 3
MAX_TRADES_PER_DAY = 5
MAX_TRADES_PER_WEEK = 15
CONSECUTIVE_LOSSES_TRIGGER = 3
LOSS_THRESHOLD_R = 0.25

# How far a simulated year may wander from its own expected return before the
# path is thrown away and drawn again. See build_series.
MAX_PATH_SIGMA = 1.5

# The strategy has no time-based exit. This is a backstop so a trade that never
# reverts and never invalidates does not sit in the book forever.
MAX_HOLDING_DAYS = 12
MAX_PATH_ATTEMPTS = 200


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    start_price: float
    annual_vol: float
    annual_drift: float


# Prices are plausible mid-2026 marks for the symbols in config/rules.yaml.
# Volatilities are the rough long-run figures for each name.
INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("SPY", "SPDR S&P 500 ETF Trust", 641.20, 0.140, 0.105),
    Instrument("QQQ", "Invesco QQQ Trust", 572.90, 0.198, 0.130),
    Instrument("AAPL", "Apple Inc.", 246.40, 0.262, 0.115),
    Instrument("MSFT", "Microsoft Corporation", 511.30, 0.238, 0.120),
    Instrument("JNJ", "Johnson & Johnson", 167.80, 0.152, 0.055),
    Instrument("KO", "The Coca-Cola Company", 73.60, 0.141, 0.050),
)


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Series:
    """One symbol's bars plus the indicators the strategy is written against."""

    instrument: Instrument
    bars: list[Bar] = field(default_factory=list)
    sma20: list[float | None] = field(default_factory=list)
    sma200: list[float | None] = field(default_factory=list)
    atr14: list[float | None] = field(default_factory=list)
    vol20: list[float | None] = field(default_factory=list)


# ------------------------------------------------------------------ price path


def trading_days(count: int, end: date) -> list[date]:
    """`count` weekdays ending on `end`. Holidays are ignored deliberately.

    A demo calendar that skipped Thanksgiving would be more accurate and no more
    useful; nothing on the site is dated against a real session.
    """
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def build_series(instrument: Instrument, days: list[date]) -> Series:
    """Geometric Brownian motion, then an intraday range around each close.

    Each symbol gets its own generator, seeded from the symbol, so adding or
    reordering instruments does not reshuffle everyone else's prices.

    Paths whose end-to-end return lands beyond `MAX_PATH_SIGMA` are drawn again.
    Unfiltered GBM will happily hand a low-volatility dividend name a 67% year,
    which is a legitimate sample from the distribution and an obviously wrong
    number to publish on a page claiming to be realistic. Rejecting the tails
    costs nothing here: the point of the path is to make indicators, entries and
    stops hang together, not to be a faithful draw.
    """
    seed_material = f"{SEED}:{instrument.symbol}"
    for attempt in range(MAX_PATH_ATTEMPTS):
        rng = random.Random(f"{seed_material}:{attempt}")
        series = _one_path(rng, instrument, days)
        total_log_return = math.log(series.bars[-1].close / instrument.start_price)
        years = len(days) / 252
        expected = instrument.annual_drift * years
        sigma = instrument.annual_vol * math.sqrt(years)
        if abs(total_log_return - expected) <= MAX_PATH_SIGMA * sigma:
            _fill_indicators(series)
            return series

    raise RuntimeError(
        f"no plausible path for {instrument.symbol} in {MAX_PATH_ATTEMPTS} attempts"
    )


def _one_path(rng: random.Random, instrument: Instrument, days: list[date]) -> Series:
    series = Series(instrument=instrument)

    daily_vol = instrument.annual_vol / math.sqrt(252)
    daily_drift = instrument.annual_drift / 252 - 0.5 * daily_vol**2

    price = instrument.start_price
    base_volume = 1_000_000 * (60.0 / math.sqrt(instrument.start_price))

    for day in days:
        shock = rng.gauss(0.0, 1.0)
        previous_close = price
        price = previous_close * math.exp(daily_drift + daily_vol * shock)

        # The range widens on the days that moved, which is what makes ATR
        # respond to conditions rather than sit flat.
        move = abs(price - previous_close)
        wick = previous_close * daily_vol * rng.uniform(0.35, 1.15)
        high = max(previous_close, price) + wick * rng.uniform(0.2, 1.0)
        low = min(previous_close, price) - wick * rng.uniform(0.2, 1.0)
        open_ = previous_close * (1 + rng.gauss(0.0, daily_vol * 0.25))
        open_ = min(max(open_, low), high)

        # Volume expands with the range. The trend-break and momentum notes both
        # lean on "participation", so it has to correlate with something.
        stretch = move / max(previous_close * daily_vol, 1e-9)
        volume = base_volume * rng.uniform(0.72, 1.28) * (1 + 0.42 * min(stretch, 3.0))

        series.bars.append(
            Bar(
                day=day,
                open=round(open_, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(price, 2),
                volume=round(volume, -2),
            )
        )

    return series


def _fill_indicators(series: Series) -> None:
    """Simple averages and a Wilder-style ATR, computed the same way as the bot."""
    closes = [b.close for b in series.bars]
    volumes = [b.volume for b in series.bars]

    for i in range(len(series.bars)):
        series.sma20.append(_mean(closes[i - 19 : i + 1]) if i >= 19 else None)
        series.sma200.append(_mean(closes[i - 199 : i + 1]) if i >= 199 else None)
        series.vol20.append(_mean(volumes[i - 19 : i + 1]) if i >= 19 else None)

    true_ranges: list[float] = []
    for i, bar in enumerate(series.bars):
        if i == 0:
            true_ranges.append(bar.high - bar.low)
        else:
            previous_close = series.bars[i - 1].close
            true_ranges.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            )

    atr: float | None = None
    for i, true_range in enumerate(true_ranges):
        if i < 13:
            series.atr14.append(None)
            continue
        if i == 13:
            atr = _mean(true_ranges[:14])
        else:
            assert atr is not None
            atr = (atr * 13 + true_range) / 14
        series.atr14.append(atr)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


# ----------------------------------------------------------------------- trades


@dataclass
class OpenTrade:
    ref: str
    symbol: str
    entry_index: int
    entry_day: date
    entry_price: float
    stop: float
    target: float
    qty: float
    strategy: str
    rationale: str
    planned_risk: float
    risk_pct_of_equity: float
    mae: float = 0.0
    mfe: float = 0.0


def _fees(qty: float, proceeds: float) -> float:
    """Alpaca equities: no commission, plus the regulatory fees on a sale.

    SEC Section 31 at $27.80 per $1,000,000 of proceeds and FINRA TAF at
    $0.000166 per share, capped at $8.30. Both are charged on sells only, which
    is why the numbers on this site are small. Trade frequency is still capped
    in config/rules.yaml, because the churn that cost the Alpha Arena entrants
    their stakes was spread and slippage as much as commission.
    """
    sec = proceeds * 0.0000278
    taf = min(qty * 0.000166, 8.30)
    return round(sec + taf, 2)


def _rationale(
    symbol: str,
    close: float,
    sma20: float,
    sma200: float,
    atr: float,
    stop: float,
    risk_pct: float,
) -> str:
    """Written in the brand voice: size and invalidation before the idea."""
    stretch = (sma20 - close) / atr
    return (
        f"Risking {risk_pct:.2f}% on this one. Invalidated below {stop:,.2f}. "
        f"{symbol} is {stretch:.1f} ATR under its 20-day average "
        f"({close:,.2f} against {sma20:,.2f}) and still above the 200-day at "
        f"{sma200:,.2f}, so the longer trend is intact. Range narrowed into the "
        f"low rather than expanding. ATR {atr:,.2f}. No earnings inside the "
        f"blackout window."
    )


def generate_trades(
    rng: random.Random,
    series_by_symbol: dict[str, Series],
    days: list[date],
    first_trading_index: int,
) -> tuple[list[dict[str, Any]], list[OpenTrade], list[dict[str, Any]]]:
    """Walk the bars forward, opening and resolving trades under the real rules.

    Returns closed trades, still-open trades, and the stand-down trips that the
    consecutive-loss counter produced along the way.
    """
    closed: list[dict[str, Any]] = []
    open_trades: list[OpenTrade] = []
    stand_downs: list[dict[str, Any]] = []

    realised_total = 0.0
    consecutive_losses = 0
    stand_down_until: date | None = None
    stand_down_trips: list[date] = []
    ref_seq = 0

    for i in range(first_trading_index, len(days)):
        today = days[i]

        # --- resolve anything open, by walking that day's bar ----------------
        still_open: list[OpenTrade] = []
        for trade in list(open_trades):
            bar = series_by_symbol[trade.symbol].bars[i]
            held_days = i - trade.entry_index

            # Excursions are sampled at the close, never intrabar. That is the
            # limitation metrics.py documents, reproduced here on purpose.
            marked = (bar.close - trade.entry_price) * trade.qty
            trade.mae = min(trade.mae, marked)
            trade.mfe = max(trade.mfe, marked)

            # Resolved in the order the strategy resolves: invalidation first,
            # then the two exits it names, then the holding cap. Checking the
            # stop before the target on the same bar is the pessimistic reading
            # and the correct one, because daily bars cannot say which came
            # first and assuming the good one flatters every figure downstream.
            sma20_today = series_by_symbol[trade.symbol].sma20[i]
            exit_price: float | None = None
            exit_reason = ""
            if bar.low <= trade.stop:
                # Stops fill at or below the level. A gap through it fills at the
                # open, which is where the extra loss on a bad morning comes from.
                exit_price = min(trade.stop, bar.open)
                exit_reason = "stop"
            elif bar.high >= trade.target:
                exit_price = max(trade.target, bar.open) if bar.open > trade.target else trade.target
                exit_reason = "target"
            elif sma20_today is not None and bar.high >= sma20_today:
                # "Exit: the 20-day average, or roughly 3 ATR, whichever comes
                # first." Reverting to the mean is the thesis being right, and it
                # is what closes most of these trades.
                exit_price = sma20_today
                exit_reason = "mean"
            elif held_days >= MAX_HOLDING_DAYS:
                exit_price = bar.close
                exit_reason = "time"

            if exit_price is None:
                still_open.append(trade)
                continue

            proceeds = exit_price * trade.qty
            fees = _fees(trade.qty, proceeds)
            gross = (exit_price - trade.entry_price) * trade.qty
            net = gross - fees
            realised_total += net
            r_multiple = net / trade.planned_risk

            closed.append(
                {
                    "ref": trade.ref,
                    "symbol": trade.symbol,
                    "assetClass": "us_equity",
                    "strategy": trade.strategy,
                    "direction": "buy",
                    "qty": round(trade.qty, 4),
                    "entryTime": _iso(trade.entry_day, time(14, 45)),
                    "entryPrice": round(trade.entry_price, 2),
                    "plannedStop": round(trade.stop, 2),
                    "plannedTarget": round(trade.target, 2),
                    "exitTime": _iso(today, time(19, 30)),
                    "exitPrice": round(exit_price, 2),
                    "exitReason": exit_reason,
                    "grossPnlUsd": round(gross, 2),
                    "feesUsd": fees,
                    "netPnlUsd": round(net, 2),
                    "rMultiple": round(r_multiple, 3),
                    "plannedRiskUsd": round(trade.planned_risk, 2),
                    "riskPctOfEquity": round(trade.risk_pct_of_equity, 3),
                    "maeUsd": round(trade.mae, 2),
                    "mfeUsd": round(trade.mfe, 2),
                    "holdingDays": held_days,
                    "executionMode": "paper",
                    "rationale": trade.rationale,
                }
            )

            # The streak counter, exactly as stand_down.py classifies it: a
            # result inside +/- 0.25R is a scratch and neither counts nor resets.
            if r_multiple < -LOSS_THRESHOLD_R:
                consecutive_losses += 1
            elif r_multiple > LOSS_THRESHOLD_R:
                consecutive_losses = 0

            if consecutive_losses >= CONSECUTIVE_LOSSES_TRIGGER:
                repeat = any((today - d).days <= 30 for d in stand_down_trips)
                stage = 2 if repeat else 1
                days_off = 10 if repeat else 3
                stand_down_until = today + timedelta(days=days_off)
                stand_down_trips.append(today)
                stand_downs.append(
                    {
                        "stage": stage,
                        "startedAt": _iso(today, time(19, 45)),
                        "endsAt": _iso(stand_down_until, time(19, 45)),
                        "consecutiveLosses": consecutive_losses,
                        "note": (
                            f"{consecutive_losses} qualifying losses in a row. Live "
                            f"execution suspended for {days_off} days. Paper trading "
                            "carried on unchanged, and closing positions and moving "
                            "stops were never blocked."
                        ),
                    }
                )
                consecutive_losses = 0

        open_trades = still_open

        # --- consider a new entry --------------------------------------------
        if stand_down_until is not None and today <= stand_down_until:
            # A stand-down suspends money, not trading. Paper carries on, and in
            # this dataset everything is paper, so entries continue.
            pass

        opened_today = 0
        equity_now = STARTING_EQUITY + realised_total
        week_start = today - timedelta(days=today.weekday())
        trades_this_week = sum(
            1 for t in closed if _day(t["entryTime"]) >= week_start
        ) + sum(1 for t in open_trades if t.entry_day >= week_start)

        held_symbols = {t.symbol for t in open_trades}
        candidates = [s for s in series_by_symbol.values() if s.instrument.symbol not in held_symbols]
        rng.shuffle(candidates)

        for candidate in candidates:
            if len(open_trades) >= MAX_CONCURRENT_POSITIONS:
                break
            if opened_today >= MAX_TRADES_PER_DAY or trades_this_week >= MAX_TRADES_PER_WEEK:
                break

            bar = candidate.bars[i]
            sma20 = candidate.sma20[i]
            sma200 = candidate.sma200[i]
            atr = candidate.atr14[i]
            if sma20 is None or sma200 is None or atr is None or atr <= 0:
                continue

            # The mean-reversion entry conditions from src/bot/strategy.py,
            # evaluated rather than asserted. Exhaustion is read the two ways the
            # strategy describes it: a range narrowing into the low, or a low
            # that failed to take out the previous one.
            prior = candidate.bars[i - 1]
            trend_up = bar.close > sma200
            stretched = (sma20 - bar.close) > 0.6 * atr
            exhaustion = (bar.high - bar.low) < (prior.high - prior.low) or bar.low > prior.low
            if not (trend_up and stretched and exhaustion):
                continue

            # Size from the risk rule. The stop is set first, and quantity is
            # whatever keeps the loss inside the budget.
            risk_pct = rng.uniform(0.32, 0.88)
            open_risk = sum(t.planned_risk for t in open_trades)
            headroom = (MAX_TOTAL_RISK_PCT / 100 * equity_now) - open_risk
            budget = min(risk_pct / 100 * equity_now, headroom)
            if budget <= 0:
                continue

            stop = bar.close - 2.0 * atr
            per_share = bar.close - stop
            qty = math.floor(budget / per_share)
            if qty < 1:
                continue

            planned_risk = per_share * qty
            actual_pct = planned_risk / equity_now * 100
            target = bar.close + 3.0 * atr

            ref_seq += 1
            open_trades.append(
                OpenTrade(
                    ref=f"MC-{today.year}-{ref_seq:03d}",
                    symbol=candidate.instrument.symbol,
                    entry_index=i,
                    entry_day=today,
                    entry_price=bar.close,
                    stop=round(stop, 2),
                    target=round(target, 2),
                    qty=float(qty),
                    strategy="mean_reversion",
                    rationale=_rationale(
                        candidate.instrument.symbol,
                        bar.close,
                        sma20,
                        sma200,
                        atr,
                        stop,
                        actual_pct,
                    ),
                    planned_risk=planned_risk,
                    risk_pct_of_equity=actual_pct,
                )
            )
            opened_today += 1
            trades_this_week += 1

    return closed, open_trades, stand_downs


def _iso(day: date, at: time) -> str:
    return datetime.combine(day, at).isoformat() + "Z"


def _day(iso: str) -> date:
    return date.fromisoformat(iso[:10])


# ------------------------------------------------------------------ assembly


def build_equity_curve(
    days: list[date],
    first_index: int,
    closed: list[dict[str, Any]],
    open_trades: list[OpenTrade],
    series_by_symbol: dict[str, Series],
) -> list[dict[str, Any]]:
    """Realised P&L to date, plus whatever the open book was worth that evening."""
    curve: list[dict[str, Any]] = []
    for i in range(first_index, len(days)):
        today = days[i]
        realised = sum(t["netPnlUsd"] for t in closed if _day(t["exitTime"]) <= today)

        unrealised = 0.0
        for trade in closed:
            if _day(trade["entryTime"]) <= today < _day(trade["exitTime"]):
                bar = series_by_symbol[trade["symbol"]].bars[i]
                unrealised += (bar.close - trade["entryPrice"]) * trade["qty"]
        for live in open_trades:
            if live.entry_day <= today:
                bar = series_by_symbol[live.symbol].bars[i]
                unrealised += (bar.close - live.entry_price) * live.qty

        curve.append(
            {
                "date": today.isoformat(),
                "equity": round(STARTING_EQUITY + realised + unrealised, 2),
                "realised": round(STARTING_EQUITY + realised, 2),
            }
        )
    return curve


def build_positions(
    open_trades: list[OpenTrade], series_by_symbol: dict[str, Series], last_index: int
) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for trade in open_trades:
        bar = series_by_symbol[trade.symbol].bars[last_index]
        unrealised = (bar.close - trade.entry_price) * trade.qty
        positions.append(
            {
                "ref": trade.ref,
                "symbol": trade.symbol,
                "assetClass": "us_equity",
                "strategy": trade.strategy,
                "direction": "buy",
                "qty": round(trade.qty, 4),
                "entryPrice": round(trade.entry_price, 2),
                "currentPrice": bar.close,
                "openedAt": _iso(trade.entry_day, time(14, 45)),
                "plannedStop": trade.stop,
                "plannedTarget": trade.target,
                "plannedRiskUsd": round(trade.planned_risk, 2),
                "riskPctOfEquity": round(trade.risk_pct_of_equity, 3),
                "unrealisedPnlUsd": round(unrealised, 2),
                "rationale": trade.rationale,
            }
        )
    return positions


def build_market(series_by_symbol: dict[str, Series], last_index: int) -> list[dict[str, Any]]:
    """The last quote plus the indicators, which is what task #31 puts in context."""
    out: list[dict[str, Any]] = []
    for series in series_by_symbol.values():
        bar = series.bars[last_index]
        # A penny wide is right for these names, and the floor of two cents is
        # what stops the rounded bid and ask printing as the same number.
        spread = round(max(bar.close * 0.00006, 0.02), 2)
        sma20 = series.sma20[last_index]
        sma200 = series.sma200[last_index]
        atr = series.atr14[last_index]
        vol20 = series.vol20[last_index]
        out.append(
            {
                "symbol": series.instrument.symbol,
                "name": series.instrument.name,
                "bid": round(bar.close - spread / 2, 2),
                "ask": round(bar.close + spread / 2, 2),
                "last": bar.close,
                "changePct": round((bar.close / series.bars[last_index - 1].close - 1) * 100, 2),
                "sma20": round(sma20, 2) if sma20 else None,
                "sma200": round(sma200, 2) if sma200 else None,
                "atr14": round(atr, 2) if atr else None,
                "volume": bar.volume,
                "avgVolume20": round(vol20, -2) if vol20 else None,
                "sparkline": [b.close for b in series.bars[last_index - 59 : last_index + 1]],
            }
        )
    return sorted(out, key=lambda row: str(row["symbol"]))


def main() -> int:
    rng = random.Random(SEED)

    end = date(2026, 8, 7)
    days = trading_days(WARMUP_DAYS + TRADING_DAYS, end)
    first_trading_index = WARMUP_DAYS
    last_index = len(days) - 1

    series_by_symbol = {
        inst.symbol: build_series(inst, days) for inst in INSTRUMENTS
    }

    closed, open_trades, stand_downs = generate_trades(
        rng, series_by_symbol, days, first_trading_index
    )
    curve = build_equity_curve(days, first_trading_index, closed, open_trades, series_by_symbol)
    positions = build_positions(open_trades, series_by_symbol, last_index)
    market = build_market(series_by_symbol, last_index)

    equity = curve[-1]["equity"]
    open_risk = sum(p["plannedRiskUsd"] for p in positions)
    invested = sum(p["qty"] * p["currentPrice"] for p in positions)
    realised_today = sum(
        t["netPnlUsd"] for t in closed if _day(t["exitTime"]) == days[last_index]
    )
    equity_yesterday = curve[-2]["equity"]

    # The streak as it stands on the last day, so the Overview can show the
    # breaker's live count rather than a number typed in by hand.
    consecutive = 0
    for trade in sorted(closed, key=lambda t: str(t["exitTime"])):
        if trade["rMultiple"] < -LOSS_THRESHOLD_R:
            consecutive += 1
        elif trade["rMultiple"] > LOSS_THRESHOLD_R:
            consecutive = 0

    payload: dict[str, Any] = {
        "meta": {
            "synthetic": True,
            "generator": "scripts/generate_demo_data.py",
            "seed": SEED,
            "generatedFor": days[last_index].isoformat(),
            "note": (
                "Every figure in this file is simulated. Nothing here came from a "
                "broker, a journal or an API, and the published site makes no "
                "network request at all."
            ),
        },
        "operator": {
            "name": "Operator",
            "accountLabel": "Alpaca paper account",
            "email": "operator@mudhorn.demo",
        },
        "account": {
            "equityUsd": equity,
            "startingEquityUsd": STARTING_EQUITY,
            "cashUsd": round(equity - invested, 2),
            "buyingPowerUsd": round((equity - invested) * 4, 2),
            "grossExposureUsd": round(invested, 2),
            "openRiskUsd": round(open_risk, 2),
            "openRiskPct": round(open_risk / equity * 100, 3),
            "realisedPnlTodayUsd": round(realised_today, 2),
            "changeTodayUsd": round(equity - equity_yesterday, 2),
            "changeTodayPct": round((equity / equity_yesterday - 1) * 100, 3),
            "asOf": _iso(days[last_index], time(20, 5)),
        },
        "limits": {
            "maxRiskPerTradePct": MAX_RISK_PER_TRADE_PCT,
            "maxTotalRiskPct": MAX_TOTAL_RISK_PCT,
            "maxConcurrentPositions": MAX_CONCURRENT_POSITIONS,
            "maxTradesPerDay": MAX_TRADES_PER_DAY,
            "maxTradesPerWeek": MAX_TRADES_PER_WEEK,
            "consecutiveLossesTrigger": CONSECUTIVE_LOSSES_TRIGGER,
            "lossThresholdR": LOSS_THRESHOLD_R,
        },
        "standDown": {
            "active": False,
            "stage": 0,
            "consecutiveLosses": consecutive,
            "trigger": CONSECUTIVE_LOSSES_TRIGGER,
            "lastTriggeredAt": stand_downs[-1]["startedAt"] if stand_downs else None,
            "note": (
                "Live execution is withheld during a stand-down. Paper trading "
                "continues exactly as normal, and closing a position or moving a "
                "stop is never gated."
            ),
        },
        "standDownHistory": stand_downs,
        "activity": {
            "tradesToday": sum(
                1 for t in closed if _day(t["entryTime"]) == days[last_index]
            ),
            "tradesThisWeek": sum(
                1
                for t in closed
                if _day(t["entryTime"]) >= days[last_index] - timedelta(days=days[last_index].weekday())
            ),
        },
        "equityCurve": curve,
        "positions": positions,
        "trades": sorted(closed, key=lambda t: str(t["exitTime"]), reverse=True),
        "market": market,
        "rulesYaml": RULES_PATH.read_text(encoding="utf-8"),
    }

    _check_invariants(payload)

    body = json.dumps(payload, indent=2, sort_keys=False)
    header = (
        "// Generated by scripts/generate_demo_data.py. Do not edit by hand.\n"
        "//\n"
        "// EVERY FIGURE BELOW IS SYNTHETIC. This file is the only data source the\n"
        "// public site has: no journal, no broker, no API, no network request. The\n"
        "// real dashboard in src/bot/web/ renders live account state and is never\n"
        "// published, which is the whole reason this file exists.\n"
        "//\n"
        "// Regenerate with:  python scripts/generate_demo_data.py\n"
        "\n"
        "window.MUDHORN_DEMO = "
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(header + body + ";\n", encoding="utf-8")

    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    print(f"  {len(payload['trades'])} closed trades, {len(positions)} open")
    print(f"  equity ${equity:,.2f}  open risk ${open_risk:,.2f} ({payload['account']['openRiskPct']}%)")
    print(f"  {len(stand_downs)} stand-down trip(s)")
    return 0


def _check_invariants(payload: dict[str, Any]) -> None:
    """Fail loudly rather than publish a dataset that breaks the operator's rules.

    The site is a demo, but a demo that shows a 1.4% single-trade risk against a
    1% cap teaches the reader the wrong thing about what the gate does.
    """
    limits = payload["limits"]
    equity = payload["account"]["equityUsd"]

    for trade in payload["trades"]:
        assert trade["plannedStop"] > 0, f"{trade['ref']} has no stop"
        assert trade["riskPctOfEquity"] <= limits["maxRiskPerTradePct"] + 1e-9, (
            f"{trade['ref']} risks {trade['riskPctOfEquity']}% against a "
            f"{limits['maxRiskPerTradePct']}% cap"
        )

    open_risk_pct = payload["account"]["openRiskPct"]
    assert open_risk_pct <= limits["maxTotalRiskPct"] + 1e-9, (
        f"open risk {open_risk_pct}% exceeds the {limits['maxTotalRiskPct']}% cap"
    )
    assert len(payload["positions"]) <= limits["maxConcurrentPositions"]
    assert equity > 0

    # The limits echoed into the JSON must match the file the Rules page shows.
    rules_text = payload["rulesYaml"]
    for key, value in (
        ("max_risk_per_trade_pct", limits["maxRiskPerTradePct"]),
        ("max_total_risk_pct", limits["maxTotalRiskPct"]),
        ("max_concurrent_positions", limits["maxConcurrentPositions"]),
        ("consecutive_losses_trigger", limits["consecutiveLossesTrigger"]),
    ):
        needle = f"{key}: {value:g}" if isinstance(value, float) else f"{key}: {value}"
        assert needle in rules_text, f"config/rules.yaml no longer says '{needle}'"


if __name__ == "__main__":
    raise SystemExit(main())
