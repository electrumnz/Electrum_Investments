"""The Tailscale expiry check.

The failure being guarded: the node key lapses, the Funnel and the private
address stop answering, and the bot carries on trading normally with nothing
anywhere saying why. Ninety days of notice followed by an outage.

The assertions worth having are the ones about not lying — a check that stopped
running must report unknown rather than healthy, and an expiry that is genuinely
absent must report disabled rather than zero days left.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bot import tailnet
from bot.tailnet import WARN_WITHIN_DAYS, TailnetStatus, main, parse, read

NOW = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)


def status_json(expiry: datetime | None, state: str = "Running") -> dict[str, Any]:
    self_node: dict[str, Any] = {"HostName": "Mudhorn"}
    if expiry is not None:
        self_node["KeyExpiry"] = expiry.isoformat()
    return {"BackendState": state, "Self": self_node}


# ------------------------------------------------------------------ parsing


def test_a_healthy_link_far_from_expiry_needs_nothing():
    """The real reading from this droplet: 90 days out."""
    status = parse(status_json(NOW + timedelta(days=90)), now=NOW)

    assert status.logged_in
    assert not status.needs_attention(now=NOW)
    assert status.days_remaining(now=NOW) == pytest.approx(90, abs=0.01)


def test_the_warning_fires_inside_the_notice_period():
    status = parse(status_json(NOW + timedelta(days=WARN_WITHIN_DAYS - 1)), now=NOW)

    assert status.needs_attention(now=NOW)
    assert "expires in 9 days" in status.headline(now=NOW)
    # Says what breaks, not just that something will.
    assert "stop answering" in status.headline(now=NOW)


def test_the_warning_does_not_fire_a_day_early():
    """A banner that is always on is furniture, and stops being read."""
    status = parse(status_json(NOW + timedelta(days=WARN_WITHIN_DAYS + 1)), now=NOW)

    assert not status.needs_attention(now=NOW)


def test_an_expired_key_says_so_in_the_past_tense():
    status = parse(status_json(NOW - timedelta(days=1)), now=NOW)

    assert status.needs_attention(now=NOW)
    assert "EXPIRED" in status.headline(now=NOW)


def test_being_logged_out_is_reported_even_with_no_expiry_date():
    """The state after `tailscale logout`, or after the key already lapsed."""
    status = parse({"BackendState": "NeedsLogin", "Self": {}}, now=NOW)

    assert not status.logged_in
    assert status.needs_attention(now=NOW)
    assert "not logged in" in status.headline(now=NOW)


def test_a_missing_expiry_means_disabled_not_zero():
    """The good outcome. Reading it as "expires now" would cry wolf forever."""
    status = parse(status_json(None), now=NOW)

    assert status.expiry_disabled
    assert status.days_remaining(now=NOW) is None
    assert not status.needs_attention(now=NOW)
    assert "disabled" in status.headline(now=NOW)


def test_unreadable_output_is_an_error_rather_than_a_clean_bill():
    """A missing or broken tailscale binary is exactly what to report."""
    status = parse(None, now=NOW)

    assert status.needs_attention(now=NOW)
    assert "could not be read" in status.headline(now=NOW)


def test_a_schema_change_costs_a_reading_rather_than_raising():
    """That JSON is not this project's to control, and this runs in a timer."""
    status = parse({"BackendState": 42, "Self": "not a dict"}, now=NOW)

    assert status.needs_attention(now=NOW)


def test_an_unreadable_self_node_is_unknown_rather_than_disabled():
    """The good outcome must not be what a failed reading looks like.

    `expiry_disabled` is `logged_in and key_expires_at is None`, and the expiry
    lives on the `Self` node — so a payload with a healthy `BackendState` and no
    usable `Self` came back as "Tailscale key expiry is disabled. Nothing to
    do.", `needs_attention` False, and `main` exiting 0. A check that could not
    read the node reported the link as permanently safe.

    The test above passed over this because it garbled `BackendState` at the
    same time, so the attention came from the mangled state rather than from the
    unreadable node. Both halves are pinned separately here, with the state left
    healthy so only the `Self` node is doing the work.
    """
    for payload in ({"BackendState": "Running"}, {"BackendState": "Running", "Self": 7}):
        status = parse(payload, now=NOW)

        assert not status.expiry_disabled
        assert status.days_remaining(now=NOW) is None
        assert status.needs_attention(now=NOW)
        assert "could not be read" in status.headline(now=NOW)
        assert "unknown rather than disabled" in status.headline(now=NOW)


def test_funnel_hostnames_are_picked_up_when_present():
    serve = {"AllowFunnel": {"mudhorn.tailc04415.ts.net:443": True, "other:443": False}}

    status = parse(status_json(NOW + timedelta(days=90)), serve, now=NOW)

    assert status.funnel_hostnames == ["mudhorn.tailc04415.ts.net"]


# ------------------------------------------------------------------ staleness


def test_a_check_that_stopped_running_reports_unknown_not_healthy():
    """The trap. A stale file describing a healthy link is not evidence of one."""
    status = TailnetStatus(
        checked_at=NOW - timedelta(days=5),
        backend_state="Running",
        key_expires_at=NOW + timedelta(days=85),
    )

    assert status.is_stale(now=NOW)
    assert status.needs_attention(now=NOW)
    assert "unknown rather than healthy" in status.headline(now=NOW)


def test_a_recent_check_is_not_stale():
    status = TailnetStatus(
        checked_at=NOW - timedelta(hours=6),
        backend_state="Running",
        key_expires_at=NOW + timedelta(days=85),
    )

    assert not status.is_stale(now=NOW)
    assert not status.needs_attention(now=NOW)


def test_never_having_run_reads_as_nothing_rather_than_as_fine(tmp_path: Path):
    """A box without the timer installed must not be told its link is healthy."""
    assert read(tmp_path / "absent.json") is None


def test_a_corrupt_status_file_reads_as_nothing(tmp_path: Path):
    path = tmp_path / "tailnet.json"
    path.write_text("{ truncated", encoding="utf-8")

    assert read(path) is None


def test_a_reading_survives_the_round_trip(tmp_path: Path):
    original = parse(status_json(NOW + timedelta(days=42)), now=NOW)
    path = tmp_path / "tailnet.json"
    path.write_text(original.to_json(), encoding="utf-8")

    restored = read(path)

    assert restored is not None
    assert restored.key_expires_at == original.key_expires_at
    assert restored.backend_state == "Running"


# ----------------------------------------------------------------- the CLI


def _run(tmp_path: Path, payload: dict[str, Any] | None) -> tuple[int, Path]:
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(payload) if payload else "", encoding="utf-8")
    out = tmp_path / "tailnet-status.json"
    code = main(["--status", str(status_file), "--out", str(out)])
    return code, out


def test_the_cli_exits_non_zero_so_systemd_records_a_failure(tmp_path: Path):
    """The backstop channel, for when nobody has opened the dashboard."""
    code, out = _run(tmp_path, {"BackendState": "NeedsLogin", "Self": {}})

    assert code == 1
    assert out.exists()


def test_the_cli_exits_zero_when_the_link_is_healthy(tmp_path: Path):
    code, out = _run(tmp_path, status_json(datetime.now(UTC) + timedelta(days=90)))

    assert code == 0
    assert read(out) is not None


def test_the_cli_still_writes_a_reading_when_tailscale_produced_nothing(tmp_path: Path):
    """Otherwise the previous file stays in place, looking current."""
    code, out = _run(tmp_path, None)

    assert code == 1
    written = read(out)
    assert written is not None
    assert written.needs_attention()


# ------------------------------------------------------------------- clearing


def test_re_checking_clears_a_warning_once_the_link_is_fixed(tmp_path: Path):
    """The banner must not outlive the thing it warned about.

    A warning still on screen after the fix teaches an operator to ignore the
    next one, so re-running the check has to be able to take it away and not
    only put it up.
    """
    code, out = _run(tmp_path, {"BackendState": "NeedsLogin", "Self": {}})
    assert code == 1
    warned = read(out)
    assert warned is not None and warned.needs_attention()

    # `tailscale up` has happened; the same check runs again.
    status_file = tmp_path / "fixed.json"
    status_file.write_text(
        json.dumps(status_json(datetime.now(UTC) + timedelta(days=90))), encoding="utf-8"
    )
    assert main(["--status", str(status_file), "--out", str(out)]) == 0

    healthy = read(out)
    assert healthy is not None
    assert not healthy.needs_attention()


def test_clear_removes_the_reading_entirely(tmp_path: Path):
    """For a handover or a removed timer: nothing is the honest state, not an all-clear."""
    _, out = _run(tmp_path, {"BackendState": "NeedsLogin", "Self": {}})
    assert out.exists()

    assert main(["--clear", "--out", str(out)]) == 0

    assert not out.exists()
    assert read(out) is None


def test_clear_is_harmless_when_there_is_nothing_to_clear(tmp_path: Path):
    assert main(["--clear", "--out", str(tmp_path / "never-written.json")]) == 0


def test_the_banner_names_the_command_that_clears_it():
    """An operator who has just fixed it should not have to guess."""
    from bot.tailnet import RECHECK_COMMAND

    logged_out = parse({"BackendState": "NeedsLogin", "Self": {}}, now=NOW)
    expiring = parse(status_json(NOW + timedelta(days=3)), now=NOW)

    assert RECHECK_COMMAND in logged_out.headline(now=NOW)
    assert RECHECK_COMMAND in expiring.headline(now=NOW)


# ------------------------------------------ what the Funnel actually serves


def _serve(
    host: str = "mudhorn.tailc04415.ts.net:443",
    proxy: str = "http://127.0.0.1:8787",
) -> dict[str, Any]:
    """The shape `tailscale serve status --json` returns."""
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": proxy}}}},
        "AllowFunnel": {host: True},
    }


def _status() -> dict[str, Any]:
    return {
        "BackendState": "Running",
        "Self": {"KeyExpiry": "2026-11-01T00:00:00Z"},
    }


def test_a_funnel_pointed_at_the_wrong_port_is_a_positive_finding():
    """The live failure, reproduced. Measured 13 Aug 2026.

    Every dashboard path on the public URL answered 404 from `server: uvicorn`
    while `POST /mcp` answered 401 — the Funnel was proxying to the MCP server.
    Throughout, `mudhorn-web` was active and healthy on loopback and this module
    reported the link in perfect health, because the key had not expired and a
    key is all it looked at.
    """
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    reading = tailnet.parse(_status(), _serve(proxy="http://127.0.0.1:9090"), now=now)

    assert reading.serves_the_dashboard is False
    assert reading.needs_attention(now=now)
    headline = reading.headline(now=now)
    assert "NOT this dashboard" in headline
    # Both ports named, so the reader can see which way round it is wrong.
    assert "9090" in headline
    assert "8787" in headline
    assert tailnet.SERVE_STATUS_COMMAND in headline


def test_a_correctly_pointed_funnel_says_nothing_about_it():
    """A caveat that fires on a healthy box is furniture."""
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    reading = tailnet.parse(_status(), _serve(), now=now)

    assert reading.serves_the_dashboard is True
    assert not reading.needs_attention(now=now)
    assert "NOT this dashboard" not in reading.headline(now=now)


def test_no_serve_output_is_COULD_NOT_ASK_and_never_a_clean_bill():
    """`None`, not True, and it must not escalate either.

    An unreadable `tailscale serve status` and a correctly pointed Funnel are
    different facts, and only one of them is reassuring. But a check that
    escalated on its own inability to read would fire on every box where the
    serve output is simply not collected, which is how a real warning gets
    trained out of a reader.
    """
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    for serve in (None, {}, "not an object", {"Web": "garbled"}):
        reading = tailnet.parse(_status(), serve, now=now)
        assert reading.serves_the_dashboard is None, serve
        assert not reading.needs_attention(now=now), serve


def test_a_host_served_but_NOT_funnelled_cannot_vouch_for_the_public_url():
    """A private mapping says nothing about what the public URL does.

    Counting it would let a correct tailnet-only handler vouch for a Funnel
    pointed somewhere else entirely.
    """
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    serve = {
        "Web": {
            "private.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}},
            "public.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9090"}}},
        },
        "AllowFunnel": {"public.ts.net:443": True},
    }
    reading = tailnet.parse(_status(), serve, now=now)

    assert reading.funnel_targets == {"public.ts.net": "http://127.0.0.1:9090"}
    assert reading.serves_the_dashboard is False


def test_an_unparseable_target_is_left_out_rather_than_guessed_at():
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    reading = tailnet.parse(_status(), _serve(proxy="not-a-target"), now=now)
    # Present but unreadable as a port: it cannot claim the dashboard is served.
    assert reading.serves_the_dashboard is False


def test_the_wrong_funnel_is_reported_AHEAD_of_a_pending_expiry():
    """An outage happening now outranks notice of one in ten days.

    A banner leading with "the key expires in 8 days" above a URL already
    serving somebody else's app buries the live fault under the pending one.
    """
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    soon = {
        "BackendState": "Running",
        "Self": {"KeyExpiry": "2026-08-21T00:00:00Z"},
    }
    headline = tailnet.parse(soon, _serve(proxy="http://127.0.0.1:9090"), now=now).headline(now=now)
    assert "NOT this dashboard" in headline
    assert "expires in" not in headline


def test_the_could_not_ask_state_survives_the_round_trip(tmp_path):
    """An empty mapping on disk would come back as "the Funnel points nowhere".

    That is a claim; absence is the honest lack of one. A reading written
    before this field existed has no key at all and must read as `None`.
    """
    now = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
    out = tmp_path / "tailnet-status.json"

    unknown = tailnet.parse(_status(), None, now=now)
    out.write_text(unknown.to_json(), encoding="utf-8")
    restored_unknown = tailnet.read(out)
    assert restored_unknown is not None
    assert restored_unknown.serves_the_dashboard is None

    known = tailnet.parse(_status(), _serve(proxy="http://127.0.0.1:9090"), now=now)
    out.write_text(known.to_json(), encoding="utf-8")
    restored = tailnet.read(out)
    assert restored is not None
    assert restored.funnel_targets == {
        "mudhorn.tailc04415.ts.net": "http://127.0.0.1:9090"
    }
    assert restored.serves_the_dashboard is False

    # And a record written before the field existed.
    out.write_text(json.dumps({"checked_at": now.isoformat(), "backend_state": "Running"}))
    older = tailnet.read(out)
    assert older is not None
    assert older.serves_the_dashboard is None
