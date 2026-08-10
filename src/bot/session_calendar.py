"""Which days the market trades, and until what time — held in the background.

`market_clock.py` computes Alpaca's session phases from New York time and gets
daylight saving right without a diary entry. What arithmetic over a clock
structurally cannot know is which days are *skipped* and which end *early*, and
those two are not equally visible:

- **A holiday reads as a suspicious silence.** Nothing fills, no bars arrive,
  and something eventually looks wrong.
- **An early close does not.** On the Friday after Thanksgiving, Christmas Eve
  and 3rd July the market shuts at 13:00 New York. `_phase_at` says 16:00 for
  three more hours, the session was genuinely open, and every figure stays
  plausible. That is the shape of failure this repository exists to refuse.

So the calendar is fetched — once a day, not once a cycle — and everything else
here is a pure lookup over what was fetched.

**Nothing in the trading path reads this and nothing here gates anything.** It
is a network-derived cache, and `RiskGate` has to stay deterministic and must
not fail open on one. The gate's own weekday-shaped session check is unchanged
and still does not know about holidays; this makes the gap *visible* to the
operator and to the model rather than closing it inside the gate.

**An unfetched calendar answers `None`, never "no trading days".** Same rule as
`FinnhubCalendar.is_degraded`, `Broker.orders_degraded` and `has_cycles` in
`news_history`: a question that could not be asked must not come back looking
like an answer. Every query here returns `None` for "not known" and callers are
expected to render that difference rather than collapse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import structlog

from .market_clock import NY, TradingDay

__all__ = ["SessionCalendar"]

log = structlog.get_logger()

#: How far ahead to fetch. One call covers a quarter, which is far more than
#: anything reads and costs the same as a week. The calendar is published a year
#: or more in advance and effectively never changes, so there is no freshness
#: argument for a shorter horizon.
HORIZON_DAYS = 120

#: Fetched at most this often. The data changes on the scale of exchange
#: announcements, not minutes, so tying this to the decision loop's fifteen
#: minutes would be 96 identical calls a day for one day's worth of answer.
REFRESH_AFTER = timedelta(hours=20)

#: How long to wait before trying again after a FAILED fetch, which is a
#: separate question from how long a successful one stays fresh.
#:
#: Without this the retry is unbounded, and the reason is subtle: a failure
#: leaves `_fetched_at` unset, so "have we ever fetched" stays false and every
#: caller re-attempts. Observed against `MockBroker`, which has no calendar at
#: all — the web poller reads every five seconds, so it made five failed
#: attempts in ten seconds and would have carried on all day. Against real
#: Alpaca that is a hot retry loop aimed at an endpoint that has just shown it
#: is unhappy, which is the worst moment to hammer it.
#:
#: Much shorter than `REFRESH_AFTER`, because a transient failure should heal
#: within the hour rather than wait for tomorrow.
RETRY_AFTER = timedelta(minutes=15)


@dataclass
class SessionCalendar:
    """A cached view of Alpaca's trading calendar, refreshed at most daily.

    Mutable by design — it is a cache with a refresh — and deliberately the only
    stateful thing in the session-hours story. Everything it is asked is a pure
    lookup over `_days`.
    """

    _days: dict[date, TradingDay] = field(default_factory=dict)
    _fetched_at: datetime | None = None
    _attempted_at: datetime | None = None
    _covers_until: date | None = None
    _last_attempt_failed: bool = False

    # ------------------------------------------------------------- refreshing

    def refresh(self, broker: object, now: datetime | None = None) -> bool:
        """Fetch if the cache is stale or does not reach far enough ahead.

        Returns True when a fetch happened, so a caller can log it once rather
        than inferring it. Never raises: `get_calendar` catches its own errors
        and answers `None`, and a calendar failure must not end a cycle that
        would otherwise have traded.
        """
        now = now or datetime.now(UTC)
        if not self._needs_refresh(now):
            return False

        self._attempted_at = now
        today = now.astimezone(NY).date()
        end = today + timedelta(days=HORIZON_DAYS)
        get = getattr(broker, "get_calendar", None)
        days = get(today - timedelta(days=1), end) if callable(get) else None

        # `None` is "could not ask" and MUST NOT clear what is already held. A
        # stale calendar is enormously better than none — the dates in it were
        # published months ago and have not moved — so a failed refresh keeps
        # the old answers and only marks that the last attempt failed.
        if days is None:
            self._last_attempt_failed = True
            log.warning("session_calendar_refresh_failed", held=len(self._days))
            return False

        self._days = {d.date: d for d in days}
        self._fetched_at = now
        self._covers_until = end
        self._last_attempt_failed = False
        log.info(
            "session_calendar_refreshed",
            days=len(self._days),
            through=end.isoformat(),
            early_closes=len([d for d in days if d.is_early_close]),
        )
        return True

    def _needs_refresh(self, now: datetime) -> bool:
        # Backing off a FAILURE, which is a different clock from freshness. This
        # is checked first because after a failure `_fetched_at` may still be
        # None, and "never fetched" would otherwise re-attempt on every call.
        if (
            self._last_attempt_failed
            and self._attempted_at is not None
            and now - self._attempted_at < RETRY_AFTER
        ):
            return False
        if self._fetched_at is None:
            return True
        if now - self._fetched_at >= REFRESH_AFTER:
            return True
        # A horizon that has walked forward past what was fetched. Without this
        # the cache would silently stop answering about the far end rather than
        # going back for more.
        if self._covers_until is not None:
            wanted = now.astimezone(NY).date() + timedelta(days=HORIZON_DAYS // 2)
            if wanted > self._covers_until:
                return True
        return False

    # -------------------------------------------------------------- reporting

    @property
    def is_loaded(self) -> bool:
        return self._fetched_at is not None

    @property
    def is_degraded(self) -> bool:
        """True when the answers on offer are not known to be current.

        Covers both "never loaded" and "loaded once, and the last attempt to
        refresh failed". A caller must say so rather than presenting a stale
        holiday list as this morning's.
        """
        return not self.is_loaded or self._last_attempt_failed

    @property
    def fetched_at(self) -> datetime | None:
        return self._fetched_at

    # ---------------------------------------------------------------- queries

    def day(self, when: date) -> TradingDay | None:
        """The session on that date, or `None`.

        `None` is genuinely ambiguous here — not a trading day, or outside the
        fetched range, or never fetched — so callers must consult `is_loaded`
        and `covers` rather than reading it as "holiday".
        """
        return self._days.get(when)

    def covers(self, when: date) -> bool:
        """Whether an answer about this date is available at all."""
        if not self.is_loaded or self._covers_until is None:
            return False
        earliest = min(self._days) if self._days else self._covers_until
        return earliest <= when <= self._covers_until

    def is_trading_day(self, when: date) -> bool | None:
        """True, False, or `None` for "cannot say"."""
        if not self.covers(when):
            return None
        return when in self._days

    def next_trading_day(self, after: date) -> TradingDay | None:
        upcoming = sorted(d for d in self._days if d > after)
        return self._days[upcoming[0]] if upcoming else None

    def upcoming(self, when: date, count: int = 5) -> list[TradingDay]:
        """The next `count` sessions from `when` inclusive. Empty if unknown."""
        return [self._days[d] for d in sorted(self._days) if d >= when][:count]

    def unusual_days(self, when: date, count: int = 3) -> list[TradingDay]:
        """Upcoming sessions that do NOT run the standard 09:30-16:00.

        The whole point of holding the calendar. A half-day is invisible while
        it is happening, so it is worth surfacing before it arrives rather than
        only answering about today.
        """
        return [d for d in self._days.values() if d.date >= when and d.is_unusual][
            :count
        ]

    def render(self, when: date, count: int = 5) -> list[str]:
        """Quick reference: the next few sessions with their real hours.

        States what it does not know instead of printing a short list, because a
        truncated calendar and a quiet fortnight look identical on the page.
        """
        if not self.is_loaded:
            return [
                "- Trading calendar NOT loaded, so holidays and early closes "
                "are unknown. The hours above assume an ordinary session."
            ]

        lines: list[str] = []
        if self._last_attempt_failed:
            lines.append(
                "- CALENDAR STALE: the last refresh failed. The dates below "
                "were published well in advance and rarely change, but they "
                "are not confirmed current."
            )
        if self.is_trading_day(when) is False:
            following = self.next_trading_day(when)
            resumes = following.render() if following else "an unknown date"
            lines.append(
                f"- {when:%a %d %b} is NOT a trading day (market holiday). "
                f"Trading resumes {resumes}."
            )
        else:
            today = self.day(when)
            if today is not None and today.is_early_close:
                lines.append(
                    f"- EARLY CLOSE TODAY: the session ends "
                    f"{today.close_local:%H:%M} New York, not 16:00. Everything "
                    "else here assumes a 16:00 close, so a position expecting "
                    "the usual afternoon has three hours less than it looks."
                )

        upcoming = self.upcoming(when, count)
        if upcoming:
            lines.append("- Next sessions: " + "; ".join(d.render() for d in upcoming))

        ahead = [d for d in self.unusual_days(when) if d.date != when]
        if ahead:
            lines.append(
                "- Non-standard hours coming up: "
                + "; ".join(d.render() for d in ahead)
            )
        return lines
