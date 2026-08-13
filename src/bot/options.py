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
        """Cash needed to take delivery if a call is exercised or assigned.

        This is the figure that decides whether Alpaca forces a liquidation:
        if the account cannot cover it, the position is sold out inside the
        hour before expiry rather than exercised.

        **`abs(qty)` because cash at stake is a magnitude.** A short position
        arrives from the broker with a NEGATIVE quantity, and multiplying it
        straight through produced a negative cost — which then made
        `buying_power_usd >= cost` true for any account, so the most dangerous
        position on the book reported that it could fund itself. A quantity's
        sign says which side of the trade this is, and that is a separate
        question from how much money the expiry moves; `assess_expiry` answers
        the side separately rather than smuggling it in here.

        Zero for a put, and the zero is real rather than missing: exercising a
        put DELIVERS shares and receives cash, so no buying power is needed.
        What that leaves behind is a stock position, and the message says so —
        a figure of zero must not be rendered as "this costs nothing".
        """
        if self.right != OptionRight.CALL:
            return 0.0
        return self.strike * CONTRACT_MULTIPLIER * abs(qty)


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

    # `OptionContract.strike` is `Field(gt=0)`, so an OCC-shaped symbol whose
    # strike field is all zeroes made this function RAISE a `ValidationError`
    # out of a docstring that promises `None` for anything it cannot parse.
    #
    # That matters well beyond this module. `models.class_key_for_symbol` calls
    # here to decide which instrument class a symbol belongs to, and so does
    # `RiskGate._option_expiry` — so a symbol arriving from a model proposal or
    # from an adopted dream's `symbols` list could raise straight out of the
    # gate. A gate that can fail is a gate that can fail open, and a parser with
    # two possible answers must not have a third.
    #
    # Checked here rather than by widening the field, because `gt=0` is correct:
    # a zero-strike contract is not a contract, so the honest answer is "this is
    # not an option symbol" and never a contract with a nonsense strike.
    strike = int(match.group("strike")) / 1000.0
    if strike <= 0:
        return None

    return OptionContract(
        symbol=symbol.strip().upper(),
        underlying=match.group("root"),
        expiry=expiry,
        right=OptionRight.CALL if match.group("right") == "C" else OptionRight.PUT,
        strike=strike,
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
    #: `None` means the question does not apply or could not be answered, and it
    #: is deliberately not `True`. See `assess_expiry`: funding only decides
    #: anything for a LONG CALL, and answering `True` anywhere else was the
    #: reassuring reading at the moment it was least warranted.
    can_fund_exercise: bool | None = None
    #: A negative quantity is a WRITTEN option. Alpaca assigns those rather than
    #: exercising them, which is a different event with a different consequence,
    #: so the two must not share one sentence.
    is_short: bool = False
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

    # A short position arrives from the broker with a negative quantity, and it
    # is a genuinely different expiry event: Alpaca ASSIGNS a written option
    # that finishes in the money rather than exercising it.
    is_short = qty < 0

    # Funding only decides an outcome for a LONG CALL — that is the one case
    # where the account has to find cash to take delivery, and the one case
    # where being unable to find it makes Alpaca liquidate inside the final
    # hour. Everywhere else the comparison answers a question nobody asked:
    #
    #   - a long PUT costs nothing to exercise, so `0 >= cost` was trivially
    #     True and rendered as reassurance about a position that leaves a SHORT
    #     stock holding behind it;
    #   - a SHORT call used to compare against a NEGATIVE cost, so every
    #     account on earth could "fund" the most dangerous position on the book.
    #
    # `None` is the honest answer for both. Same rule as everywhere else here:
    # a question that does not apply must not come back looking like the
    # comfortable answer to it.
    can_fund = (
        None
        if buying_power_usd is None
        or is_short
        or contract.right is not OptionRight.CALL
        else buying_power_usd >= cost
    )

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
        is_short=is_short,
        message=_message(contract, urgency, days, itm, cost, can_fund, is_short),
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
    is_short: bool = False,
) -> str:
    side = "short " if is_short else ""
    label = f"{side}{contract.underlying} {contract.strike:g} {contract.right.value}"
    expiry = contract.expiry.isoformat()

    if urgency == ExpiryUrgency.NONE:
        return f"{label} expires {expiry} ({days:.1f} days) — nothing due yet."

    # A WRITTEN option is assigned, not exercised, and the difference is not
    # wording: assignment is done TO the account, it cannot be declined, and it
    # is the one branch where "Do Not Exercise" is irrelevant because the choice
    # was never the holder's. It also never has a funding answer — see
    # `assess_expiry` — so this must not fall through to the branches below,
    # which read the funding flag.
    if itm and is_short:
        lands = (
            f"the stock is PUT TO the account at {contract.strike:g}, so it must "
            "find the cash"
            if contract.right is OptionRight.PUT
            else f"the stock is CALLED AWAY at {contract.strike:g}, so it must "
            "deliver shares it may not hold"
        )
        return (
            f"ACTION NEEDED — {label} expires {expiry} ({days:.2f} days) and is "
            f"in the money, so it will be AUTO-ASSIGNED after the close. "
            f"Assignment is done TO the account and cannot be declined: {lands}. "
            f"Whether the account can carry what that leaves is NOT established "
            f"here. Close it first if that is not what you want."
        )

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

    if itm and contract.right is OptionRight.PUT:
        # A long put costs nothing to exercise, so the call branch's
        # "costing $0" read as harmless. It is not: exercising DELIVERS the
        # stock, and an account that does not hold it ends up SHORT.
        return (
            f"ACTION NEEDED — {label} expires {expiry} ({days:.2f} days) and is "
            f"in the money, so it will be AUTO-EXERCISED at 6pm ET. That "
            f"DELIVERS the stock at {contract.strike:g} — leaving a SHORT stock "
            f"position if the account does not already hold the shares — rather "
            f"than costing cash. Close it first if that is not what you want — "
            f"Do Not Exercise cannot be filed through the API."
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
