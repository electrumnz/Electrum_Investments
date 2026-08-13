"""The startup banners, and the one property a unit test could not see.

Both defects these cover were live on 13 Aug 2026 with the whole suite green,
because both are about a claim the operator is told rather than a value a
function returns:

- `web/app.py` printed the auth banner to **stdout** and then called
  `uvicorn.run()`, which blocks forever. systemd gives a service a pipe rather
  than a terminal, Python block-buffers stdout, and an 8 KiB buffer holding 200
  bytes is never flushed by a process that does not exit. Observed in the
  journal: a restart at 06:54:25 with the newest banner stamped 06:41:04, which
  was the PREVIOUS process flushing on its way out.
- `Env.inference_provider` was a property with five tests and a docstring
  calling itself "the line a startup banner prints". **Nothing outside
  `config.py` ever called it.**

So the tests below are deliberately of two kinds, and the second kind is the
one that matters.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from bot.announce import announce_dashboard_auth, announce_inference, say
from bot.config import Env

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(**overrides: Any) -> Env:
    return Env(_env_file=None, **overrides)  # type: ignore[call-arg]


# --------------------------------------------------- what the sentences say


def test_the_auth_banner_names_which_mode_is_in_force(capsys):
    announce_dashboard_auth(_env(DASHBOARD_PASSWORD="hunter2"))
    assert "SET" in capsys.readouterr().err

    announce_dashboard_auth(_env(DASHBOARD_PASSWORD=""))
    assert "NOT set" in capsys.readouterr().err


def test_the_auth_banner_never_renders_the_password(capsys):
    """Same rule as the Settings page: configured or not configured, never the
    value. This goes into a journal somebody may paste."""
    announce_dashboard_auth(_env(DASHBOARD_PASSWORD="correct-horse-battery"))
    assert "correct-horse-battery" not in capsys.readouterr().err


def test_the_provider_banner_never_renders_the_key(capsys):
    secret = "doo_v1_super_secret_model_access_key"
    announce_inference(_env(DO_INFERENCE_KEY=secret))
    assert secret not in capsys.readouterr().err


def test_a_banner_cannot_take_the_process_down(capsys):
    """It runs before anything else does, so it must not be able to raise.

    A diagnostic that killed the loop would be a far worse failure than a
    diagnostic nobody read.
    """

    class Exploding:
        def write(self, _: str) -> int:
            raise OSError("journal is gone")

        def flush(self) -> None:
            raise OSError("journal is gone")

    say("anything", stream=Exploding())  # type: ignore[arg-type]


# ------------------------------------- the property a unit test cannot see


def test_a_banner_arrives_before_the_process_blocks():
    """**The test that would have failed yesterday, and the only shape of it
    that would.**

    Every assertion above passes against a plain `print()` to stdout, because
    `capsys` reads the buffer directly. What broke in production was that the
    text never left the buffer: the service printed, then blocked in
    `uvicorn.run()` and did not exit for hours.

    So this runs a REAL subprocess, with `PYTHONUNBUFFERED` removed from its
    environment — systemd sets no such variable and neither unit does — and
    reads the output while the child is still alive. A buffered write is
    invisible here exactly as it was invisible in the journal.
    """
    src = str(REPO_ROOT / "src")
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {src!r})
        from bot.announce import announce_dashboard_auth
        from bot.config import Env
        announce_dashboard_auth(Env(_env_file=None, DASHBOARD_PASSWORD="x"))
        time.sleep(30)          # stand in for uvicorn.run(), which never returns
        """
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},  # no PYTHONUNBUFFERED
    )
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        assert "Dashboard password is SET" in line, (
            "the banner did not arrive while the process was still running, "
            "which is exactly how it vanished under systemd"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ----------------------------------------- and that somebody actually calls it


@pytest.mark.parametrize(
    "entrypoint",
    ["cmd_loop", "cmd_dream", "cmd_confer"],
)
def test_every_long_running_entrypoint_announces_its_provider(entrypoint: str):
    """`Env.inference_provider` was fully tested and never called.

    Five tests asserted what the sentence said and none asserted that anybody
    said it, so the feature was absent while reading as covered — the same
    shape as `Dream.is_offerable`. This asserts the CALL, parsed out of the
    module rather than trusted, so deleting the line is a red build.
    """
    tree = ast.parse((REPO_ROOT / "src" / "bot" / "main.py").read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == entrypoint
    )
    called = {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "announce_inference" in called, f"{entrypoint} never says which provider it is on"


def test_the_web_entrypoint_announces_both():
    """The dashboard is the one process where BOTH facts matter: which provider
    its chat agents reach, and whether anyone needs a password to ask them."""
    tree = ast.parse((REPO_ROOT / "src" / "bot" / "web" / "app.py").read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"announce_dashboard_auth", "announce_inference"} <= called


def test_no_long_running_entrypoint_uses_a_bare_print():
    """`print()` defaults to stdout, which systemd block-buffers.

    The fix is a function rather than a `flush=True` precisely so this can be
    asserted: the next entrypoint written with a plain `print` fails here rather
    than shipping a banner that appears when the process dies.
    """
    tree = ast.parse((REPO_ROOT / "src" / "bot" / "web" / "app.py").read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    bare = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and not any(kw.arg == "flush" for kw in node.keywords)
    ]
    assert not bare, (
        "a bare print() in a process that then blocks forever is invisible "
        "under systemd until it exits; use announce.say"
    )
