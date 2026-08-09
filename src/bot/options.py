"""Option contract parsing and expiry safety.

This module exists for one reason: **Alpaca will close or exercise your option
positions at expiry without asking**, and the ways it does that are all
surprising if you are not watching the calendar.

From Alpaca's own documentation:

- An option in the money by **$0.01 or more** is **automatically exercised** by
  6:00 PM ET on expiration day.
- If the account lacks the buying power to fund that exercise, Alpaca
  **liquidates the position within the hour before expiry**.
- A short position in the money by $0.01 or more is **automatically assigned**
  after the close.
- "Do Not Exercise" **cannot be submitted through the API** — it requires
  contacting Alpaca support.

That last point is what makes this a hard rule rather than a nice-to-have.
Because DNE is not automatable, the only programmatic way to avoid an unwanted
exercise, assignment or forced liquidation is to **close the position yourself,
early enough that you are choosing the exit rather than having it chosen**.

Scope note: the bot does not propose option trades — options remain deferred as
their own piece of work, because Greeks, spreads and assignment each need gates
of their own. This module is purely protective. It watches option positions
that exist in the account however they got there, and makes sure an expiry
never arrives unannounced.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

# US options expire and settle on Eastern time regardless of where you are.
MARKET_TZ = ZoneInfo("America/New_York")

# Alpaca auto-exercises anything this far in the money.
AUTO_EXERCISE_THRESHOLD_USD = 0.01

# Alpaca liquidates un-fundable ITM positions inside this window before expiry.
FORCED_LIQUIDATION_WINDOW = timedelta(hours=1)

# US equity options stop trading at the close on expiration day.
EXPIRY_CLOSE_ET = time(16, 0)

# One contract controls this many shares. Options are quoted per share, so a
# $2.50 premium is $250, and exercising a $580 call needs $58,000 — the number
# that decides whether Alpaca liquidates you instead.
CONTRACT_MULTIPLIER = 100

# OCC format: root, YYMMDD, C or P, then strike x 1000 in 8 digits.
# e.g. SPY260918C00580000 -> SPY, 2026-09-18, call, $580.00
_OCC_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class ExpiryUrgency(StrEnum):
    """How close an option is to something happening to it automatically."""

    NONE = "none"          # comfortably far out
    WATCH = "watch"        # inside the warning window
    URGENT = "urgent"      # expires today or tomorrow
    CRITICAL = "critical"  # inside the forced-liquidation window


class OptionContract(BaseModel):
    """A parsed OCC option symbol."""

    symbol: str
    underlying: str
    expiry: date
    right: OptionRight
    strike: float = Field(gt=0)

    @property
    def expiry_close_utc(self) -> datetime:
        """When the contract stops trading, in UTC.

        Expiry is defined in Eastern time, so this has to convert rather than
        assume — getting it wrong by the DST offset would put the warning an
        hour late, which is exactly when it matters.
        """
        return datetime.combine(self.expiry, EXPIRY_CLOSE_ET, tzinfo=MARKET_TZ).astimezone(UTC)

    def days_to_expiry(self, now: datetime) -> float:
        return (self.expiry_close_utc - now).total_seconds() / 86_400

    def is_itm(self, underlying_price: float) -> bool:
        """In the money by Alpaca's $0.01 auto-exercise threshold.

        The epsilon is load-bearing, not decoration: `580.01 - 580.0` evaluates
        to 0.00999999... in binary floating point, so a strict comparison would
        silently fail to warn about a contract sitting exactly at the threshold.
        Rounding toward warning is the right direction for safety code.
        """
        epsilon = 1e-9
        if self.right == OptionRight.CALL:
            return underlying_price - self.strike >= AUTO_EXERCISE_THRESHOLD_USD - epsilon
        return self.strike - underlying_price >= AUTO_EXERCISE_THRESHOLD_USD - epsilon

    def exercise_cost_usd(self, qty: float) -> float:
        """Cash needed to take delivery if a long call is exercised.

        This is the figure that decides whether Alpaca forces a liquidation:
        if the account cannot cover it, the position is sold out inside the
        hour before expiry rather than exercised.
        """
        if self.right != OptionRight.CALL:
            return 0.0
        return self.strike * CONTRACT_MULTIPLIER * qty


def parse_occ_symbol(symbol: str) -> OptionContract | None:
    """Parse an OCC option symbol. Returns None if it is not one.

    Alpaca writes these without padding — `SPY260918C00580000`. Anything that
    does not match is an ordinary equity or crypto symbol.
    """
    match = _OCC_RE.match(symbol.strip().upper().replace(" ", ""))
    if not match:
        return None

    ymd = match.group("ymd")
    try:
        expiry = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None

    return OptionContract(
        symbol=symbol.strip().upper(),
        underlying=match.group("root"),
        expiry=expiry,
        right=OptionRight.CALL if match.group("right") == "C" else OptionRight.PUT,
        strike=int(match.group("strike")) / 1000.0,
    )


def is_option_symbol(symbol: str) -> bool:
    return parse_occ_symbol(symbol) is not None


class ExpiryAlert(BaseModel):
    """A warning that something automatic is about to happen to a position."""

    symbol: str
    underlying: str
    expiry: date
    right: OptionRight
    strike: float
    qty: float
    days_to_expiry: float
    urgency: ExpiryUrgency
    in_the_money: bool | None = None
    exercise_cost_usd: float = 0.0
    can_fund_exercise: bool | None = None
    message: str

    @property
    def needs_action(self) -> bool:
        return self.urgency in (ExpiryUrgency.URGENT, ExpiryUrgency.CRITICAL)


def assess_expiry(
    contract: OptionContract,
    *,
    qty: float,
    now: datetime,
    warn_days: float,
    underlying_price: float | None = None,
    buying_power_usd: float | None = None,
) -> ExpiryAlert:
    """Work out how urgent an option position's expiry is, and say why.

    `underlying_price` and `buying_power_usd` are optional because they are not
    always available — without them the alert still fires on time, it just
    cannot say whether exercise or liquidation is the likely outcome.
    """
    days = contract.days_to_expiry(now)
    time_left = contract.expiry_close_utc - now

    itm = contract.is_itm(underlying_price) if underlying_price is not None else None
    cost = contract.exercise_cost_usd(qty)
    can_fund = None if buying_power_usd is None else buying_power_usd >= cost

    if time_left <= FORCED_LIQUIDATION_WINDOW:
        urgency = ExpiryUrgency.CRITICAL
    elif days <= 1:
        urgency = ExpiryUrgency.URGENT
    elif days <= warn_days:
        urgency = ExpiryUrgency.WATCH
    else:
        urgency = ExpiryUrgency.NONE

    return ExpiryAlert(
        symbol=contract.symbol,
        underlying=contract.underlying,
        expiry=contract.expiry,
        right=contract.right,
        strike=contract.strike,
        qty=qty,
        days_to_expiry=round(days, 3),
        urgency=urgency,
        in_the_money=itm,
        exercise_cost_usd=round(cost, 2),
        can_fund_exercise=can_fund,
        message=_message(contract, urgency, days, itm, cost, can_fund),
    )


def alerts_for_positions(
    positions: list[tuple[str, float]],
    *,
    now: datetime,
    warn_days: float,
    buying_power_usd: float | None = None,
    underlying_prices: dict[str, float] | None = None,
) -> list[ExpiryAlert]:
    """Build expiry alerts for whichever held positions are options.

    Takes (symbol, qty) pairs so it works against any position source without
    importing the domain models — non-option symbols are simply skipped.
    Returned most urgent first, so the thing about to happen leads.
    """
    prices = underlying_prices or {}
    alerts: list[ExpiryAlert] = []

    for symbol, qty in positions:
        contract = parse_occ_symbol(symbol)
        if contract is None:
            continue
        alerts.append(
            assess_expiry(
                contract,
                qty=qty,
                now=now,
                warn_days=warn_days,
                underlying_price=prices.get(contract.underlying),
                buying_power_usd=buying_power_usd,
            )
        )

    order = {
        ExpiryUrgency.CRITICAL: 0,
        ExpiryUrgency.URGENT: 1,
        ExpiryUrgency.WATCH: 2,
        ExpiryUrgency.NONE: 3,
    }
    alerts.sort(key=lambda a: (order[a.urgency], a.days_to_expiry))
    return alerts


def render_alerts(alerts: list[ExpiryAlert]) -> list[str]:
    """Render alerts for the Claude context block and for logs.

    Anything needing action is prefixed so it cannot be skimmed past — this is
    the one part of the market context where missing a line has an automatic,
    irreversible consequence.
    """
    if not alerts:
        return ["- (no option positions)"]

    lines: list[str] = []
    for alert in alerts:
        marker = "!!" if alert.needs_action else "  "
        lines.append(f"- {marker} {alert.message}")
    return lines


def _message(
    contract: OptionContract,
    urgency: ExpiryUrgency,
    days: float,
    itm: bool | None,
    cost: float,
    can_fund: bool | None,
) -> str:
    label = f"{contract.underlying} {contract.strike:g} {contract.right.value}"
    expiry = contract.expiry.isoformat()

    if urgency == ExpiryUrgency.NONE:
        return f"{label} expires {expiry} ({days:.1f} days) — nothing due yet."

    # The dangerous combination: in the money, and the account cannot fund
    # taking delivery. Alpaca sells this out for you, at whatever the market
    # is doing in that final hour.
    if itm and can_fund is False:
        return (
            f"ACTION NEEDED — {label} expires {expiry} ({days:.2f} days). It is "
            f"in the money and exercise would cost ${cost:,.0f}, which the "
            f"account cannot fund. Alpaca will LIQUIDATE this position inside "
            f"the final hour unless you close it first."
        )

    if itm:
        return (
            f"ACTION NEEDED — {label} expires {expiry} ({days:.2f} days) and is "
            f"in the money, so it will be AUTO-EXERCISED at 6pm ET. That leaves "
            f"a stock position costing ${cost:,.0f}. Close it first if that is "
            f"not what you want — Do Not Exercise cannot be filed through the API."
        )

    if urgency == ExpiryUrgency.CRITICAL:
        return (
            f"CRITICAL — {label} expires {expiry} within the hour. If it finishes "
            f"even $0.01 in the money it is exercised or liquidated automatically."
        )

    if urgency == ExpiryUrgency.URGENT:
        return (
            f"URGENT — {label} expires {expiry} ({days:.2f} days). Decide today: "
            f"close it, or accept automatic exercise or assignment if it finishes "
            f"in the money."
        )

    return (
        f"{label} expires {expiry} in {days:.1f} days. Plan the exit now rather "
        f"than in the final hour."
    )
