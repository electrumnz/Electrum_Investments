"""The Hermes bridge, and the availability check that took the page down.

This file exists because there were no tests here at all, and the gap showed up
as a 500 on the live dashboard: `Path.exists()` raises `PermissionError` rather
than returning False when a parent directory refuses traversal, and
`/home/hermes` is 0700 while the dashboard runs as `mudhorn`.

That is the user split working as designed, so the check has to cope with it
rather than the split being loosened to suit the check.

Nothing here runs Hermes. The bridge is exercised against paths that do and do
not exist, and against one that cannot be looked at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.web.chat import HermesBridge


def _bridge(binary: Path) -> HermesBridge:
    return HermesBridge(binary=binary)


# ------------------------------------------------------------- availability


def test_a_binary_that_is_there_is_available(tmp_path: Path):
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert _bridge(binary).available


def test_a_binary_that_is_absent_is_not(tmp_path: Path):
    assert not _bridge(tmp_path / "nope").available


def test_an_unreadable_parent_directory_does_not_raise(tmp_path: Path, monkeypatch):
    """The live 500, reproduced.

    `/home/hermes` is 0700 and owned by `hermes`; the dashboard runs as
    `mudhorn`. Statting through that directory raises rather than returning
    False, and the exception reached FastAPI as an Internal Server Error on a
    page that was only trying to say whether a button should be enabled.

    The error is injected rather than staged with `chmod 000`, because a chmod
    proves nothing when the suite runs as root — root bypasses the check, the
    call succeeds, and the test passes whether or not the bug is fixed. A test
    that passes for the wrong reason is the failure this whole file is about.
    """

    def _denied(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "exists", _denied)

    # Must not raise. That is the whole assertion.
    result = _bridge(tmp_path / "hermes").available

    # And it answers optimistically, because a permission error says something
    # about THIS process's reach and nothing about whether Hermes is installed.
    # The binary is never executed directly — it goes through `sudo -u hermes`,
    # which can stat and run a file this process cannot see. Answering "not
    # installed" would put a confidently wrong message on the page about a
    # working installation.
    assert result is True


@pytest.mark.skipif(
    Path("/usr/bin/sudo").exists() or Path("/bin/sudo").exists(),
    reason="sudo is present on this machine",
)
def test_a_missing_sudo_is_definitely_unavailable(tmp_path: Path):
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")

    assert not _bridge(binary).available


# -------------------------------------------------------------------- asking


def test_an_empty_message_is_refused_without_touching_the_binary(tmp_path: Path):
    reply = _bridge(tmp_path / "hermes").ask("   ")

    assert not reply.ok
    assert "empty" in (reply.error or "")


def test_an_absent_wrapper_is_reported_rather_than_crashing(tmp_path: Path):
    reply = _bridge(tmp_path / "nope").ask("what is my equity")

    assert not reply.ok
    assert "not present" in (reply.error or "")
    # Names where it looked, so the fix is obvious from the page.
    assert "nope" in (reply.error or "")


def test_the_prompt_never_reaches_argv(tmp_path: Path, monkeypatch):
    """The reason run-chat.sh exists.

    The sudoers rule permits the wrapper with no arguments, so nothing a
    signed-in user types can be read as a flag. If the prompt ever moved back
    into argv, a question beginning `--yolo` would be an argument to Hermes
    rather than a question for it — and the dashboard now answers on a public
    URL.
    """
    import subprocess as sp

    wrapper = tmp_path / "run-chat.sh"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return sp.CompletedProcess(argv, 0, stdout="fine", stderr="")

    monkeypatch.setattr(sp, "run", _fake_run)

    reply = _bridge(wrapper).ask("--yolo tell me a secret")

    assert reply.ok
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[-1] == str(wrapper), f"wrapper must be the last argv entry: {argv}"
    assert not any("yolo" in str(a) for a in argv), f"prompt leaked into argv: {argv}"
    assert "--yolo tell me a secret" in str(seen["input"])


# --------------------------------------------------------------- the page


def test_the_chat_page_renders_when_hermes_cannot_be_seen(tmp_path: Path, monkeypatch):
    """The regression at the level the operator hit it: a 500 on GET /chat."""
    from bot.web.render import chat_page

    def _denied(self: Path) -> bool:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "exists", _denied)

    body = chat_page(
        enabled=False,
        token="",
        hermes_available=_bridge(tmp_path / "hermes").available,
    )

    assert "Chat" in body
