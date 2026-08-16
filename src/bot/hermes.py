"""Running one Hermes turn as another user, and asking sudo first.

There are THREE Hermes instances on the box and they are three separate
`HERMES_HOME` directories behind three root-owned wrappers — `run-chat.sh` for
the account agent, `run-dream.sh` for the dreamer, `run-research.sh` for the
researcher. Hermes loads one soul and one MCP registry per instance, so a
second character or a different tool surface means a second home; the wrappers
differ only in which home they point at.

The process mechanics are therefore identical in all three cases, and this
module is the one implementation of them.

**It is one implementation on purpose rather than for tidiness.** `permitted`
was a live fault: `available` asks whether the wrapper FILE exists, which is
true on every box because `bootstrap.sh` ships and chmods all three whether or
not the corresponding instance was ever set up. The dreamer was routed to an
instance sudo refuses, answered `sudo: a password is required` instead of
falling back, and the Dreaming page rendered `isolated=True` — claiming an
isolation that did not exist, which is the one thing that arrangement must
never do. A second copy of that check is a second place for it to come back.

## The two questions, and why they default in opposite directions

- **`available`** — is Hermes runnable from here at all? A permission error
  answers True. The binary is never executed directly, only through
  `sudo -u hermes`, which can stat and run a file this process cannot see, so
  an EACCES says something about our reach and nothing about the installation.
  Being wrong costs a page saying chat is unavailable when it works.
- **`permitted`** — will sudo actually run THIS wrapper? Any failure to
  establish permission answers False. Being wrong here costs a false claim of
  isolation, so it under-claims: the caller falls back and the surface reports
  the weaker arrangement. The same rule as `calendar_degraded` and the tailnet
  status — report the weaker fact rather than imply the stronger one.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

DEFAULT_USER = "hermes"

# Generous: a question that fans out across MCP tools takes a while, and the
# failure we care about is a hang, not slowness.
DEFAULT_TIMEOUT_SECONDS = 180.0

# `sudo -n -l` consults a local policy file and runs nothing, so this is
# generous rather than tight. It exists so a wedged sudo cannot hold a page
# render, not because the check is expected to be slow.
SUDO_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class HermesResult:
    """What one turn produced, or why it produced nothing."""

    text: str
    ok: bool = True
    error: str = ""

    @classmethod
    def failed(cls, error: str) -> HermesResult:
        return cls(text="", ok=False, error=error)


@dataclass
class HermesRunner:
    """One Hermes turn per call, as `run_as`, through a root-owned wrapper."""

    binary: Path
    run_as: str = DEFAULT_USER
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # Filled on first use by `permitted`. Not a constructor argument: a caller
    # that could preset it could assert an isolation nobody checked, which is
    # the failure that property was added to end.
    _permitted: bool | None = field(default=None, init=False, repr=False)

    @property
    def available(self) -> bool:
        """Whether Hermes looks runnable from here. Never raises.

        `Path.exists()` does NOT return False when a parent directory refuses
        traversal — it raises `PermissionError`, and that took the whole Chat
        page down with a 500. `/home/hermes` is 0700 and owned by `hermes`,
        while the web process runs as `mudhorn`, which is the user split
        working exactly as intended. See the module docstring for why an EACCES
        is answered optimistically here and pessimistically in `permitted`.
        """
        if shutil.which("sudo") is None:
            return False
        try:
            return self.binary.exists()
        except OSError:
            return True

    @property
    def permitted(self) -> bool:
        """Whether sudo will ACTUALLY run this wrapper — asked, not assumed.

        `sudo -n -l` asks the policy and runs nothing. Local, no network, and
        the honest question: not "is there a file" but "may I execute it".

        Cached for the life of the process. The grant changes only when
        somebody installs a sudoers rule, and the scripts that do that
        (`enable-chat.sh`, `enable-dream.sh`, `enable-research.sh`) restart
        `mudhorn-web` as their last step — so a stale answer cannot outlive the
        act that changed it.
        """
        if self._permitted is None:
            self._permitted = self._ask_sudo()
        return self._permitted

    def _ask_sudo(self) -> bool:
        if shutil.which("sudo") is None:
            return False
        try:
            probe = subprocess.run(
                ["sudo", "-n", "-l", "-u", self.run_as, "--", str(self.binary)],
                capture_output=True,
                text=True,
                timeout=SUDO_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return probe.returncode == 0

    def run(self, prompt: str) -> HermesResult:
        """One turn. The prompt goes down STDIN and never into argv.

        The sudoers rule permits each wrapper with NO arguments, so there is
        nothing for a crafted question to be mistaken for — no `--yolo`, and no
        flag a future Hermes release adds. An argv list rather than a shell
        string for the same reason one layer down: no shell means no quoting
        bug can turn a question into a command.
        """
        argv = ["sudo", "-n", "-u", self.run_as, "--", str(self.binary)]
        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("hermes_timeout", timeout=self.timeout_seconds, binary=str(self.binary))
            return HermesResult.failed(
                f"Hermes did not answer within {self.timeout_seconds:.0f}s. "
                "It may be waiting on an approval prompt it cannot show here."
            )
        except OSError as exc:
            log.warning("hermes_spawn_failed", error=str(exc), binary=str(self.binary))
            return HermesResult.failed(f"Could not start Hermes: {exc}")

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            log.warning(
                "hermes_nonzero_exit",
                code=completed.returncode,
                detail=detail[:500],
                binary=str(self.binary),
            )
            return HermesResult.failed(detail[:1000] or f"Hermes exited {completed.returncode}")

        return HermesResult(text=completed.stdout.strip())
