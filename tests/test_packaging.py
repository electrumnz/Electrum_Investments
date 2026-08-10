"""Guards against shipping a repository that cannot import itself.

This exists because of a real failure, not a hypothetical one. `.gitignore`
carried a bare `data/` line, meant for the SQLite journal at the repository
root. A bare pattern matches a directory of that name at **any** depth, so it
silently also excluded `src/bot/data/` — and three modules under it were never
committed while `main.py` imported them regardless.

Nothing local caught it. The files were on disk, so the tests passed, `mypy`
passed, and `git status` was clean because ignored files are not reported. It
surfaced only on a deployed machine, as `No module named bot.data.finnhub`
inside an MCP tool call, which reached the operator as an agent apologising for
a backend it could not see.

The lesson is narrow and worth encoding: a green local suite says nothing about
what is actually in the repository. These tests check the repository.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


def _tracked_files() -> set[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {REPO_ROOT / p for p in out.split("\0") if p}


def _source_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git checkout"
)
def test_every_source_file_is_committed():
    """A module on disk but not in git works locally and breaks on deploy."""
    tracked = _tracked_files()
    missing = sorted(str(p.relative_to(REPO_ROOT)) for p in _source_files() if p not in tracked)

    assert not missing, (
        "These files exist locally but are not tracked by git, so a fresh clone "
        "would not have them:\n  " + "\n  ".join(missing) + "\n\n"
        "Check .gitignore for an unanchored pattern — a bare `name/` matches that "
        "directory at any depth. Anchor it with a leading slash."
    )


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git checkout"
)
def test_no_source_file_is_gitignored():
    """Catches the same fault one step earlier, with a clearer message.

    A file can be untracked because nobody added it, or because something is
    quietly excluding it. The second is far harder to notice, so it gets its own
    assertion rather than being folded into the one above.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-v", *[str(p) for p in _source_files()]],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # check-ignore exits 0 when it matched something, 1 when it matched nothing.
    assert result.returncode != 0, (
        "These source files match a .gitignore rule:\n"
        + result.stdout
        + "\nAnchor the offending pattern with a leading slash."
    )


@pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git checkout"
)
def test_every_first_party_import_resolves_to_a_committed_module():
    """The specific shape of the bug: an import of a module that was not shipped.

    Walks every `from .x import` and `import bot.x` in the package and checks a
    corresponding file exists and is tracked. This is what would have failed the
    build the moment `main.py` grew its `from .data.finnhub import ...` line.
    """
    tracked = _tracked_files()
    package = SRC / "bot"
    problems: list[str] = []

    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            # Resolve a relative import against the importing file's package.
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            if node.module is None:
                continue

            target = base.joinpath(*node.module.split("."))
            candidates = [target.with_suffix(".py"), target / "__init__.py"]
            resolved = next((c for c in candidates if c.exists()), None)

            if resolved is None:
                problems.append(f"{path.relative_to(REPO_ROOT)} imports missing {node.module}")
            elif resolved not in tracked and package in resolved.parents:
                problems.append(
                    f"{path.relative_to(REPO_ROOT)} imports {node.module}, "
                    f"which exists but is NOT committed ({resolved.relative_to(REPO_ROOT)})"
                )

    assert not problems, "\n  ".join(["Unshippable imports:", *problems])


# ------------------------------------------- the guard that guards the runtime


def test_the_runtime_directory_guard_notices_a_file_that_only_GREW(tmp_path):
    """It compared a file LISTING, so it went blind after the first offence.

    Once `data/dreams.db` existed, a test writing rows into it appeared in the
    before-set and the after-set and was reported as nothing at all — the guard
    was strongest on a clean machine and useless on a developer's, which is the
    inverse of what a guard should be. `tests/test_loop.py` already carried a
    note saying so.
    """
    from .conftest import runtime_fingerprint

    watched = tmp_path / "data"
    watched.mkdir()
    journal = watched / "journal.db"
    journal.write_text("one row")

    before = runtime_fingerprint([watched])
    journal.write_text("one row, and a second one nobody asked for")
    after = runtime_fingerprint([watched])

    assert set(after) == set(before), "the listing alone cannot see this"
    assert after != before


def test_the_runtime_directory_guard_still_notices_a_new_file(tmp_path):
    from .conftest import runtime_fingerprint

    watched = tmp_path / "audit"
    watched.mkdir()

    before = runtime_fingerprint([watched])
    (watched / "2026-06-01.jsonl").write_text("{}\n")

    assert set(runtime_fingerprint([watched])) - set(before) == {"audit/2026-06-01.jsonl"}
