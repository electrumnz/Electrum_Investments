#!/usr/bin/env python3
"""Clone or update the reference repositories listed in reference/registry.yaml.

Clones land in reference/src/ (gitignored) so the repo stays small. The exact
commit each one resolved to is written to reference/versions.json, which IS
committed — that lockfile is what makes upstream drift show up as a git diff.

Usage:
    python scripts/fetch_reference.py              # fetch/update everything
    python scripts/fetch_reference.py buzz goose   # only these
    python scripts/fetch_reference.py --depth 50   # deeper history

Requires git on PATH and network access to github.com.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "reference" / "registry.yaml"
SRC_DIR = REPO_ROOT / "reference" / "src"
VERSIONS = REPO_ROOT / "reference" / "versions.json"

LICENSE_FILENAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "LICENCE")


def load_registry() -> list[dict[str, Any]]:
    with REGISTRY.open() as f:
        data = yaml.safe_load(f)
    repos: list[dict[str, Any]] = data.get("repos", [])
    return repos


def run_git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def clone_or_update(entry: dict[str, Any], depth: int) -> dict[str, Any]:
    name = entry["name"]
    url = entry["url"]
    ref = entry.get("ref", "main")
    target = SRC_DIR / name

    if target.exists():
        print(f"  updating {name} ...")
        fetch = run_git("fetch", "--depth", str(depth), "origin", ref, cwd=target)
        if fetch.returncode != 0:
            return {"name": name, "error": fetch.stderr.strip()[:400]}
        checkout = run_git("checkout", "--force", "FETCH_HEAD", cwd=target)
        if checkout.returncode != 0:
            return {"name": name, "error": checkout.stderr.strip()[:400]}
    else:
        print(f"  cloning {name} ...")
        clone = run_git(
            "clone", "--depth", str(depth), "--branch", ref, url, str(target)
        )
        if clone.returncode != 0:
            # A repo whose default branch is not `ref` still clones without --branch.
            fallback = run_git("clone", "--depth", str(depth), url, str(target))
            if fallback.returncode != 0:
                return {"name": name, "error": fallback.stderr.strip()[:400]}

    sha = run_git("rev-parse", "HEAD", cwd=target).stdout.strip()
    committed_at = run_git("log", "-1", "--format=%cI", cwd=target).stdout.strip()
    subject = run_git("log", "-1", "--format=%s", cwd=target).stdout.strip()

    return {
        "name": name,
        "repo": entry["repo"],
        "url": url,
        "ref": ref,
        "commit": sha,
        "commit_date": committed_at,
        "commit_subject": subject[:120],
        "license_detected": detect_license(target),
        "expected_license": entry.get("expected_license"),
    }


def detect_license(path: Path) -> str:
    """Read the licence from the clone rather than trusting the registry."""
    for filename in LICENSE_FILENAMES:
        candidate = path / filename
        if not candidate.exists():
            continue
        head = candidate.read_text(encoding="utf-8", errors="replace")[:2000].upper()

        # Commons Clause is a rider bolted onto an otherwise permissive licence
        # that forbids selling the software or a service built substantially on
        # it. It makes a project source-available rather than open source, so it
        # has to be checked FIRST — the text below it still says "Apache 2.0",
        # and reporting that alone would be actively misleading.
        if "COMMONS CLAUSE" in head:
            base = "Apache-2.0" if "APACHE" in head else "unknown-base"
            return f"{base}+CommonsClause"

        # Copyleft before permissive, and the more specific variants before the
        # general ones: "AFFERO GENERAL PUBLIC LICENSE" and "LESSER GENERAL
        # PUBLIC LICENSE" both contain neither plain-GPL nor permissive wording,
        # and mistaking either for MIT is the exact error this lockfile exists
        # to prevent.
        if "AFFERO GENERAL PUBLIC LICENSE" in head:
            return "AGPL-3.0"
        if "GNU LESSER GENERAL PUBLIC LICENSE" in head:
            return "LGPL-3.0" if "VERSION 3" in head else "LGPL"
        if "GNU GENERAL PUBLIC LICENSE" in head:
            return "GPL-3.0" if "VERSION 3" in head else "GPL"

        if "APACHE LICENSE" in head:
            return "Apache-2.0"
        if "MIT LICENSE" in head or "PERMISSION IS HEREBY GRANTED, FREE OF CHARGE" in head:
            return "MIT"
        if "MOZILLA PUBLIC LICENSE" in head:
            return "MPL-2.0"
        if "BSD " in head:
            return "BSD"
        return "present-but-unrecognised"
    return "not-found"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Only fetch these entries (default: all)")
    parser.add_argument("--depth", type=int, default=1, help="Clone depth (default: 1)")
    args = parser.parse_args()

    entries = load_registry()
    if args.names:
        wanted = set(args.names)
        entries = [e for e in entries if e["name"] in wanted]
        missing = wanted - {e["name"] for e in entries}
        if missing:
            print(f"Unknown entries: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    SRC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {len(entries)} reference repositories into {SRC_DIR}")

    results = [clone_or_update(entry, args.depth) for entry in entries]

    failures = [r for r in results if "error" in r]
    successes = [r for r in results if "error" not in r]

    # Merge into any existing lockfile so a partial run does not drop entries.
    existing: dict[str, Any] = {}
    if VERSIONS.exists():
        existing = json.loads(VERSIONS.read_text())
    by_name = {r["name"]: r for r in existing.get("repos", [])}
    for r in successes:
        by_name[r["name"]] = r

    VERSIONS.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "repos": sorted(by_name.values(), key=lambda r: str(r["name"])),
            },
            indent=2,
        )
        + "\n"
    )

    print(f"\nFetched {len(successes)}/{len(entries)}. Lockfile: {VERSIONS}")

    for r in successes:
        expected = r.get("expected_license")
        detected = r.get("license_detected")
        if expected and detected not in (expected, "present-but-unrecognised"):
            print(
                f"  ! {r['name']}: licence is {detected}, registry expected {expected}"
            )

    if failures:
        print("\nFailed:", file=sys.stderr)
        for f in failures:
            print(f"  {f['name']}: {f['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
