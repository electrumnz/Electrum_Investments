"""The console server, and the two properties that make it safe to expose.

It runs commands on the box behind one bearer token, so the arguments for it
are exactly two: **it cannot reach the broker**, and **the shell is off unless
somebody switched it on.** Both are asserted here rather than described in a
docstring, because this repository has already been caught once writing a
guarantee down and never testing it — `tests/test_grants.py` passed for weeks
over a class hard-block that did not work.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from bot import console_mcp

MODULE = Path(console_mcp.__file__)
REPO_ROOT = Path(__file__).resolve().parent.parent

# The modules that reach money. `mcp_server` is the one that exposes
# `place_order`; the rest are what an order path is built from.
FORBIDDEN = {"broker", "risk", "journal", "mcp_server", "models", "reconcile", "grants"}


def _imported_names() -> set[str]:
    """Every first-party module this file imports, at any depth of the AST.

    Parsed rather than introspected, so a deferred import inside a function is
    caught too — which is where somebody would put it.
    """
    tree = ast.parse(MODULE.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[-1])
    return found


def test_the_console_cannot_reach_the_broker():
    """**The whole safety argument, and it is structural rather than prose.**

    A console that could also trade would put arbitrary shell and the order
    path behind one token. They are different privileges — the same reasoning
    that runs chat as `hermes` rather than `mudhorn`, and puts the dreamer on
    its own Hermes instance with no MCP registry.

    Same shape as `test_a_dream_cannot_describe_an_order` and the AST check on
    `TraderPowers`.
    """
    leaked = _imported_names() & FORBIDDEN
    assert not leaked, f"the console imports {sorted(leaked)}, which reach the order path"


def test_the_shell_tool_is_absent_unless_switched_on(monkeypatch: pytest.MonkeyPatch):
    """Off is the default and off is the documented state.

    Registration is conditional at import, so the tool is not merely refused
    when disabled — it is not in the list at all, and a caller cannot see it
    to try. Same pattern as `--execute` and DASHBOARD_CHAT_TOKEN.
    """
    monkeypatch.delenv("MUDHORN_CONSOLE_SHELL", raising=False)
    assert console_mcp.shell_enabled() is False

    monkeypatch.setenv("MUDHORN_CONSOLE_SHELL", "1")
    assert console_mcp.shell_enabled() is True

    # Anything that is not exactly "1" is off. A truthy-string check would make
    # `MUDHORN_CONSOLE_SHELL=0` and `=false` both enable it, which is the
    # opposite of what somebody typing them means.
    for value in ("0", "false", "no", "", "true "):
        monkeypatch.setenv("MUDHORN_CONSOLE_SHELL", value)
        assert console_mcp.shell_enabled() is False, f"{value!r} should not enable the shell"


def test_it_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch, capsys):
    """Unlike DASHBOARD_PASSWORD, absent is NOT a supported configuration.

    A dashboard with no password leaks figures and that is correct on loopback.
    This runs commands, so there is no deployment where an ungated console is
    the right answer — and therefore no such configuration.
    """
    monkeypatch.delenv("MUDHORN_CONSOLE_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["electrum-bot-console"])

    assert console_mcp.main() == 78
    assert "refuses to start" in capsys.readouterr().err


def test_a_short_token_is_refused(monkeypatch: pytest.MonkeyPatch, capsys):
    """It is the only thing between a public URL and the trading box."""
    monkeypatch.setenv("MUDHORN_CONSOLE_TOKEN", "short")
    monkeypatch.setattr("sys.argv", ["electrum-bot-console"])

    assert console_mcp.main() == 78
    err = capsys.readouterr().err
    assert "32" in err
    assert "short" not in err, "the token was echoed into the refusal"


def test_only_known_units_can_be_named():
    """`systemctl status` accepts globs, so a caller who could pass one could
    enumerate every unit on the box. An allowlist, not a pattern."""
    assert "unknown unit" in console_mcp.service_status("sshd")
    assert "unknown unit" in console_mcp.service_status("*")
    assert "unknown unit" in console_mcp.journal_tail("mudhorn-*")


def test_output_is_clipped_from_the_start_not_the_end():
    """An error is at the END of a build log.

    Truncating the tail would leave a failed run reading as a clean one, which
    is the confident-partial-answer failure arriving through a display limit.
    """
    text = "".join(f"line {i}\n" for i in range(20_000))
    clipped = console_mcp._clip(text)

    assert len(clipped) < len(text)
    assert clipped.endswith("line 19999\n")
    assert "dropped from the start" in clipped


def test_a_failing_command_reports_rather_than_raises():
    """A non-zero exit is a RESULT: the caller asked what happened, and
    "it failed, here is stderr" is the answer. Raising would lose the output,
    which is the part worth having."""
    out = console_mcp._run(["false"])
    assert "exit=1" in out

    missing = console_mcp._run(["definitely-not-a-real-binary-xyz"])
    assert "command not found" in missing


def test_run_uses_argv_and_never_a_shell():
    """A list argv cannot be word-split, globbed, or reach `&&`, `|`, `>` or
    `$(...)`. That is what bounds even the opt-in shell tool, so it is pinned
    rather than remembered.

    Checked on the AST rather than by grepping the source, which is not
    pedantry: the first version of this test searched for the string
    `shell=True` and failed on the DOCSTRING that explains why it is never
    used. A text search over a file cannot tell a rule from its explanation.
    """
    calls = [
        node
        for node in ast.walk(ast.parse(MODULE.read_text()))
        if isinstance(node, ast.Call)
    ]
    shelled = [
        node
        for node in calls
        for kw in node.keywords
        if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
    ]
    assert not shelled, "a subprocess call passes shell=, which unbounds every argument"

    # And behaviourally: shell metacharacters stay literal.
    out = console_mcp._run(["echo", "a && rm -rf /"])
    assert "a && rm -rf /" in out


def test_the_console_entrypoint_is_registered():
    """A server nobody can launch is the `Dream.is_offerable` failure again."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'electrum-bot-console = "bot.console_mcp:main"' in pyproject


def test_the_app_dir_is_overridable_for_a_test_box():
    """Hardcoding /opt/mudhorn would make this untestable and unmovable."""
    assert console_mcp.APP_DIR == Path(os.environ.get("MUDHORN_APP_DIR", "/opt/mudhorn"))
