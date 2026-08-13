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
import importlib
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


def test_the_app_dir_defaults_to_opt_mudhorn_and_is_overridable(monkeypatch: pytest.MonkeyPatch):
    """Hardcoding the path would make this untestable and unmovable.

    Reloaded rather than compared against the same expression the module itself
    uses. The first version of this asserted
    `APP_DIR == Path(os.environ.get("MUDHORN_APP_DIR", "/opt/mudhorn"))`, which
    restates the implementation and therefore passes whatever the
    implementation says — a tautology wearing a test's clothes. `ruff` caught
    it as a Yoda condition, which was the lesser of the two problems.
    """
    monkeypatch.delenv("MUDHORN_APP_DIR", raising=False)
    assert str(importlib.reload(console_mcp).APP_DIR) == "/opt/mudhorn"

    monkeypatch.setenv("MUDHORN_APP_DIR", "/srv/elsewhere")
    assert str(importlib.reload(console_mcp).APP_DIR) == "/srv/elsewhere"

    # Leave the module as the rest of the suite expects to find it.
    monkeypatch.delenv("MUDHORN_APP_DIR", raising=False)
    importlib.reload(console_mcp)


def test_the_unit_ships_disabled_and_names_no_token():
    """Installed, never enabled, and carrying no secret.

    `bootstrap.sh` installs the unit so turning it on is one command, and does
    NOT create `/etc/mudhorn-console.env` — a token generated by a provisioning
    script is a token in a provisioning log. The unit therefore cannot start
    until somebody deliberately runs `enable-console.sh`.
    """
    unit = (REPO_ROOT / "deploy" / "systemd" / "mudhorn-console.service").read_text()
    bootstrap = (REPO_ROOT / "deploy" / "bootstrap.sh").read_text()

    assert "mudhorn-console.service" in bootstrap, "the unit is not installed by bootstrap"
    assert "enable --now mudhorn-console" not in bootstrap, "bootstrap starts the console"
    assert "systemctl enable --quiet mudhorn-console" not in bootstrap

    # The token is read from a file bootstrap does not write.
    assert "EnvironmentFile=/etc/mudhorn-console.env" in unit

    # **Directives only, never the comments.** The first version of this
    # searched the whole file for `MUDHORN_CONSOLE_TOKEN=` and matched the
    # comment showing an operator how to CREATE one — the same mistake as
    # grepping for `shell=True` and hitting the docstring that forbids it. A
    # text search over a file cannot tell a rule from its explanation, and this
    # is the second time today.
    directives = [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [d for d in directives if d.startswith("Environment=")], (
        "a literal is set inline; the token belongs in EnvironmentFile"
    )

    # Loopback only. The token is the sole gate, and a bare public port is one
    # scan away from being found.
    exec_start = next(d for d in directives if d.startswith("ExecStart="))
    assert "--port 8788" in exec_start
    assert "--host" not in exec_start, "the unit passes a host, so it may not bind loopback"


def test_the_enable_script_never_prints_the_token_in_status():
    """`--status` output is the kind of thing that gets pasted into a chat.

    Same rule as the Settings page and the startup banner: configured or not
    configured, never the value.
    """
    script = (REPO_ROOT / "deploy" / "enable-console.sh").read_text()
    status_block = script.split('if [[ "$MODE" == "status" ]]')[1].split("exit 0")[0]

    assert "MUDHORN_CONSOLE_TOKEN=" not in status_block.replace("'^MUDHORN_CONSOLE_TOKEN=", "")
    assert "at least 32 characters" in status_block
    assert "$NEW_TOKEN" not in status_block


def test_turning_it_off_says_the_funnel_outlives_the_service():
    """The failure mode somebody will otherwise hit.

    `systemctl disable --now` closes the port and leaves the public URL in
    place, so the URL starts working again the moment anyone restarts the unit.
    An --off that did not say so would read as "it is withdrawn" when it is not.
    """
    script = (REPO_ROOT / "deploy" / "enable-console.sh").read_text()
    off_block = script.split('if [[ "$MODE" == "off" ]]')[1].split("exit 0")[0]

    assert "funnel" in off_block.lower()
    assert "outlives" in off_block.lower()
