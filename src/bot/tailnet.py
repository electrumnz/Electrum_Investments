"""Whether the private link to this box is still going to be there tomorrow.

## The failure this exists for

The dashboard is reached over Tailscale — a private address, or a Funnel giving
a public HTTPS URL. Tailscale node keys expire, and this tailnet caps the
lifetime at 90 days with no option to disable it.

When that key lapses, the Funnel stops serving and the private address stops
answering. **The bot keeps trading perfectly normally throughout.** The service
stays green, the journal keeps filling, orders keep going to the broker, and the
only symptom is a URL that no longer responds — with nothing anywhere saying
why, or that it was about to happen. The documented fix is to SSH in and
re-authenticate, over a network that may itself have been the way in.

So the expiry is checked on a timer and reported while it can still be acted on.

## Why the warning goes on the dashboard, of all places

It looks backwards: the dashboard is the thing that disappears, so a warning
there is worthless the moment it matters.

That is exactly why it works. The failure is not sudden — it is 90 days of
notice followed by an outage. During the notice period the dashboard is up and
being looked at, which makes it the one surface guaranteed to be in front of the
operator *before* the link dies. A warning delivered after expiry would need a
channel that does not depend on the thing that broke, and there isn't one here.

`systemd` marking the unit failed is the backstop for the case where nobody
opened the dashboard at all.

## Missing is missing, again

A status file that stopped being written must not read as "everything is fine".
`checked_at` travels with the reading and `is_stale` is computed from it, so a
check that has silently stopped reports **unknown** rather than healthy. Same
rule as `FinnhubCalendar.is_degraded` and `reconcile`'s `risk_is_understated`.

Equally, a key expiry that is genuinely absent — a tailnet that permits
disabling it — is reported as disabled rather than as zero days remaining.

And the other half of that, which is the one that was wrong: **"expiry is
disabled" is a claim about a node we could actually read.** `KeyExpiry` missing
from a `Self` object is the good outcome. `Self` missing altogether, or arriving
as something that is not an object, is output this module did not understand —
and reading that as the good outcome makes the all-clear what an unparseable
reading looks like. Same rule as `Verification` treating an empty chain as
`unverified` rather than `sourced`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Ten days' notice, so on this tailnet's 90-day cap the banner appears around
# day 80. Chosen by the operator over a fortnight, and the reasoning is sound:
# re-authenticating takes two minutes, so the notice period only has to survive
# a holiday, and a banner that sits there for two weeks stops being read long
# before it stops being true. A warning that is always on is furniture.
WARN_WITHIN_DAYS = 10

# A check that has not run for this long is treated as no reading at all. The
# timer fires every six hours, so this is four missed runs rather than one late
# one.
STALE_AFTER_HOURS = 30

# Where the timer leaves its reading for the dashboard to pick up. Under data/
# because that is already writable by the service account and already backed up.
DEFAULT_STATUS_PATH = Path("data/tailnet-status.json")

RUNNING = "Running"

# Named in the banner itself, because the gap between fixing the link and the
# banner going away is otherwise up to six hours of an operator wondering
# whether the fix worked. A warning that outlives what it warned about teaches
# people to ignore the next one.
RECHECK_COMMAND = "systemctl start mudhorn-tailnet.service"

# What `deploy/systemd/mudhorn-web.service` binds. The Funnel has to be pointed
# HERE for the public URL to be the dashboard, and this constant is the only
# thing that makes a mismatch detectable.
#
# It is a duplicate of a number in a unit file, which is normally the wrong
# shape — but the alternative is parsing the unit at runtime, and this check has
# to work from a reading taken by a timer that may run when the unit file is not
# readable. A wrong constant here produces a FALSE ALARM naming both ports,
# which is loud and self-correcting; the failure it catches is silent and was
# not caught for an unknown length of time.
DASHBOARD_PORT = 8787

# The command that shows what is actually being served, and the one that fixes
# it. Named in the banner for the reason `RECHECK_COMMAND` is: a warning whose
# remedy the reader has to go and look up is a warning they postpone.
SERVE_STATUS_COMMAND = "tailscale serve status"

# **`funnel`, never `serve`, and that distinction cost the public URL.**
# Measured 14 Aug 2026 while fixing exactly the fault this module now detects:
# `tailscale serve --bg --set-path=/mcp 8788` printed "Removing Funnel for
# mudhorn.tailc04415.ts.net:443" and took the whole public hostname off the
# internet — including the ops MCP endpoint the deploy tooling reaches the box
# through. The handler it added was correct; the subcommand silently withdrew
# public reachability as a side effect.
#
# So the banner names the FULL command rather than saying "re-point it". A
# remedy stated as an intention is one the reader has to translate, and the
# obvious translation here is the one that causes a second outage while they
# are already fixing the first.
REPOINT_COMMAND = "tailscale funnel --bg 8787"


@dataclass(frozen=True)
class TailnetStatus:
    """One reading of the link's health."""

    checked_at: datetime
    backend_state: str = ""
    key_expires_at: datetime | None = None
    funnel_hostnames: list[str] | None = None
    # Hostname -> what the Funnel actually proxies that host to, as read out of
    # `tailscale serve status --json`. `None` means the serve output was never
    # supplied or could not be read, which is a DIFFERENT state from an empty
    # mapping — see `serves_the_dashboard`.
    funnel_targets: dict[str, str] | None = None
    error: str = ""

    @property
    def logged_in(self) -> bool:
        return self.backend_state == RUNNING

    @property
    def expiry_disabled(self) -> bool:
        """No expiry at all, which is the good outcome rather than a missing one."""
        return self.logged_in and self.key_expires_at is None and not self.error

    @property
    def serves_the_dashboard(self) -> bool | None:
        """Is the public URL actually pointed at this dashboard?

        **This is a completely separate failure from key expiry, and nothing
        watched it.** Measured live on 13 Aug 2026: every dashboard path on
        `https://mudhorn.tailc04415.ts.net` answered `404` from `server:
        uvicorn`, while `POST /mcp` answered `401` — the Funnel was proxying to
        the MCP server rather than to `mudhorn-web`. Throughout, the unit was
        `active (running)` and healthy on loopback, the checkout was clean, and
        this module reported the link in perfect health, because the key had
        not expired and a key is all it looked at.

        That is the same shape as the expiry failure it was written for —
        systemd green, journal filling, and the only symptom a URL that does
        not answer — arriving through a cause the existing check structurally
        could not see.

        Three-valued, and the third value is the whole point:

        - `True`  — a Funnel host proxies to `DASHBOARD_PORT`.
        - `False` — a Funnel host is up and proxies somewhere ELSE. A positive
          finding: the public URL is serving something that is not this.
        - `None`  — could not ask. No serve output was supplied, or none of it
          could be read, or no host has the Funnel on at all. It must NOT read
          as True: an unreadable `tailscale serve status` and a correctly
          pointed Funnel are different facts, and only one of them is
          reassuring. `FinnhubCalendar.is_degraded` again, on a different
          reading.

        A host whose target cannot be parsed is left out of the mapping rather
        than guessed at, so it lands in `None` rather than manufacturing either
        answer.
        """
        if not self.funnel_targets:
            return None
        return any(
            _target_port(target) == DASHBOARD_PORT
            for target in self.funnel_targets.values()
        )

    def days_remaining(self, *, now: datetime | None = None) -> float | None:
        if self.key_expires_at is None:
            return None
        moment = now or datetime.now(UTC)
        return (self.key_expires_at - moment).total_seconds() / 86400

    def is_stale(self, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return moment - self.checked_at > timedelta(hours=STALE_AFTER_HOURS)

    def needs_attention(self, *, now: datetime | None = None) -> bool:
        """True when a person should do something, including "the check stopped"."""
        if self.is_stale(now=now) or self.error or not self.logged_in:
            return True
        # A Funnel serving the wrong thing is an outage of the dashboard, now,
        # rather than a warning about one in ten days — so it is attention-
        # worthy on its own and does not wait for the expiry clock.
        #
        # Only `False` trips it. `None` is "could not ask", and a check that
        # escalated on its own inability to read would fire on every box where
        # the serve output is simply not collected, which is how a real warning
        # gets trained out of a reader.
        if self.serves_the_dashboard is False:
            return True
        days = self.days_remaining(now=now)
        return days is not None and days <= WARN_WITHIN_DAYS

    def headline(self, *, now: datetime | None = None) -> str:
        """One sentence, written to be read by someone who has forgotten all this."""
        if self.is_stale(now=now):
            age = (now or datetime.now(UTC)) - self.checked_at
            return (
                f"Tailscale has not been checked for {age.total_seconds() / 3600:.0f} "
                "hours, so the state of the link is unknown rather than healthy. "
                "Check mudhorn-tailnet.timer."
            )
        if self.error:
            return f"Tailscale could not be read: {self.error}"
        if not self.logged_in:
            return (
                f"Tailscale is not logged in (state: {self.backend_state or 'unknown'}). "
                "This dashboard is reachable now only because you are already "
                "connected. Run `tailscale up` on the droplet, then "
                f"`{RECHECK_COMMAND}` to clear this."
            )

        # Ahead of the expiry sentence, because this one is an outage happening
        # now and that one is notice of an outage in some days' time. A banner
        # leading with "the key expires in 47 days" above a URL that is already
        # serving somebody else's app buries the live fault under the pending
        # one.
        if self.serves_the_dashboard is False:
            served = ", ".join(
                f"{host} -> {target}"
                for host, target in sorted((self.funnel_targets or {}).items())
            )
            return (
                "The public URL is NOT this dashboard. The Funnel is up and "
                f"pointed elsewhere ({served}), while this dashboard is on port "
                f"{DASHBOARD_PORT}. Every page there answers 404 while the bot "
                f"trades normally. Run `{SERVE_STATUS_COMMAND}` to see what is "
                f"being served, then `{REPOINT_COMMAND}` to put the dashboard "
                f"back on `/`, then `{RECHECK_COMMAND}` to clear this. Use "
                "`funnel`, NOT `serve` — the `serve` subcommand removes the "
                "Funnel and takes the whole public hostname offline."
            )

        days = self.days_remaining(now=now)
        if days is None:
            return "Tailscale key expiry is disabled. Nothing to do."
        if days <= 0:
            return (
                "The Tailscale node key has EXPIRED. The Funnel and the private "
                "address are down. Run `tailscale up` on the droplet, then "
                f"`{RECHECK_COMMAND}` to clear this."
            )
        expires = self.key_expires_at.date().isoformat() if self.key_expires_at else "?"
        return (
            f"The Tailscale node key expires in {days:.0f} days, on {expires}. "
            "When it does, this dashboard and the public URL both stop answering "
            "and the bot carries on trading with nothing to say so. "
            f"Run `tailscale up` on the droplet, then `{RECHECK_COMMAND}` to "
            "re-check and clear this."
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "checked_at": self.checked_at.isoformat(),
                "backend_state": self.backend_state,
                "key_expires_at": (
                    self.key_expires_at.isoformat() if self.key_expires_at else None
                ),
                "funnel_hostnames": self.funnel_hostnames or [],
                # `null` rather than `{}` when nothing was read, so the
                # could-not-ask state survives the round trip. An empty object
                # would come back as "the Funnel points nowhere", which is a
                # claim, where absence is the honest lack of one.
                "funnel_targets": self.funnel_targets,
                "error": self.error,
            },
            indent=2,
        )


def _target_port(target: str) -> int | None:
    """The port a serve target proxies to, or `None` if it cannot be read.

    Targets look like `http://127.0.0.1:8787`, and occasionally like a bare
    `127.0.0.1:8787` or a plain port. Anything this cannot parse answers `None`
    rather than a guess: a wrong port here would either invent a mismatch or
    hide one, and both are worse than saying the target was unreadable.
    """
    tail = target.rsplit(":", 1)[-1].strip("/")
    try:
        return int(tail)
    except ValueError:
        return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse(status: Any, serve: Any = None, *, now: datetime | None = None) -> TailnetStatus:
    """Read `tailscale status --json` and, optionally, `tailscale serve status --json`.

    Tolerant of every field being absent, because the shape of that output is
    not this project's to control and a schema change should cost a reading
    rather than raise inside a timer.

    `KeyExpiry` missing from `Self` means expiry is disabled — which is why it
    is passed through as `None` and rendered as "disabled" rather than being
    defaulted to anything.

    **A missing `Self` is NOT that, and conflating the two produced an
    all-clear out of output nobody could read.** `expiry_disabled` is
    `logged_in and key_expires_at is None`, so a payload carrying
    `BackendState: Running` and no usable `Self` node came back as "Tailscale
    key expiry is disabled. Nothing to do.", with `needs_attention` False and
    the unit exiting 0 — the good outcome manufactured from a reading that
    failed. The `Self` node is where the expiry lives, so its absence means the
    expiry is UNKNOWN, and an error is recorded so every downstream reader
    (banner, exit code, headline) treats it that way.

    The existing schema-change test passed over this: it garbled `Self` *and*
    `BackendState` together, so the attention came from the mangled state
    rather than from the unreadable node.
    """
    moment = now or datetime.now(UTC)
    if not isinstance(status, dict):
        return TailnetStatus(
            checked_at=moment, error="tailscale status did not return an object"
        )

    self_node = status.get("Self")
    if not isinstance(self_node, dict):
        return TailnetStatus(
            checked_at=moment,
            backend_state=str(status.get("BackendState") or ""),
            error=(
                "tailscale status carried no readable Self node, so the key "
                "expiry is unknown rather than disabled"
            ),
        )
    expiry = _timestamp(self_node.get("KeyExpiry"))

    hostnames: list[str] = []
    # `None`, not `{}`. No serve output at all has to stay distinguishable from
    # serve output that carried no Funnel — see `serves_the_dashboard`.
    targets: dict[str, str] | None = None
    if isinstance(serve, dict):
        allow = serve.get("AllowFunnel")
        funnelled: set[str] = set()
        if isinstance(allow, dict):
            funnelled = {str(host) for host, on in allow.items() if on}
            hostnames = sorted(host.split(":")[0] for host in funnelled)

        # **What each funnelled host is actually proxied TO**, which is the
        # question `funnel_hostnames` never asked. That field said only which
        # hosts had the Funnel switched on — it was parsed, stored, and read by
        # nothing, and a Funnel pointed at the wrong port satisfies it
        # perfectly. Same shape as `Dream.is_offerable`, which was defined and
        # never called and made the dream vault inert for a week.
        web = serve.get("Web")
        if isinstance(web, dict):
            targets = {}
            for host, config in web.items():
                if str(host) not in funnelled:
                    # A host served on the tailnet but NOT funnelled is not the
                    # public URL, so it says nothing about whether the public
                    # URL is right. Counting it would let a correct private
                    # mapping vouch for a broken public one.
                    continue
                handlers = (
                    config.get("Handlers") if isinstance(config, dict) else None
                )
                if not isinstance(handlers, dict):
                    continue
                for handler in handlers.values():
                    proxy = (
                        handler.get("Proxy") if isinstance(handler, dict) else None
                    )
                    if isinstance(proxy, str) and proxy:
                        targets[str(host).split(":")[0]] = proxy
                        break

    return TailnetStatus(
        checked_at=moment,
        backend_state=str(status.get("BackendState") or ""),
        key_expires_at=expiry,
        funnel_hostnames=hostnames,
        funnel_targets=targets,
    )


def read(path: Path | None = None) -> TailnetStatus | None:
    """The last reading, or `None` if the check has never run.

    `None` and "stale" are different states and both are different from healthy:
    never-run means the timer was not installed, stale means it stopped.
    """
    target = path or DEFAULT_STATUS_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    checked = _timestamp(payload.get("checked_at"))
    if checked is None:
        return None

    hostnames = payload.get("funnel_hostnames")
    # A reading written before this field existed has no key here, and that
    # comes back `None` — could not ask — rather than an empty mapping claiming
    # the Funnel points nowhere.
    stored_targets = payload.get("funnel_targets")
    targets = (
        {str(k): str(v) for k, v in stored_targets.items()}
        if isinstance(stored_targets, dict)
        else None
    )
    return TailnetStatus(
        checked_at=checked,
        backend_state=str(payload.get("backend_state") or ""),
        key_expires_at=_timestamp(payload.get("key_expires_at")),
        funnel_hostnames=[str(h) for h in hostnames] if isinstance(hostnames, list) else [],
        funnel_targets=targets,
        error=str(payload.get("error") or ""),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI for the timer. Reads the two JSON files, writes one, exits non-zero on trouble."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", type=Path)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument(
        "--clear",
        action="store_true",
        help=(
            "Delete the reading and exit. The dashboard then shows nothing "
            "rather than a warning, which is the correct state for a link "
            "whose health is unknown."
        ),
    )
    args = parser.parse_args(argv)

    # A reset rather than a re-check, for the case where the timer is being
    # removed or the box is being handed over. Deleting the file is the honest
    # reset: `read` returns None and the banner disappears, instead of a
    # written-in all-clear that would be believed for six hours whatever the
    # link is actually doing.
    if args.clear:
        args.out.unlink(missing_ok=True)
        print(f"cleared {args.out}")
        return 0

    if args.status is None:
        parser.error("--status is required unless --clear is given")

    def _load(path: Path | None) -> Any:
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    status = parse(_load(args.status), _load(args.serve))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(status.to_json(), encoding="utf-8")
    # World-readable: the dashboard runs as `mudhorn` and this check as root.
    args.out.chmod(0o644)

    line = status.headline()
    print(line)

    # Non-zero so `systemctl --failed` shows it, for the case where nobody has
    # opened the dashboard. The banner is the primary channel; this is the
    # backstop.
    return 1 if status.needs_attention() else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
