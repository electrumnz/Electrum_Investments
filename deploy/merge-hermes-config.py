#!/usr/bin/env python3
"""Merge `deploy/hermes-config.yaml` into a live `~/.hermes/config.yaml`.

## Why this exists

`hermes-config.yaml` opens by telling you to append it:

    cat /opt/mudhorn/deploy/hermes-config.yaml >> ~/.hermes/config.yaml

That is correct only for a config that does not already define
`approvals:`, `mcp_servers:`, `agent:` or `skills:`. On this droplet the live
file defines **all four**, because Hermes writes a fully-populated config on
first run. Appending there produces four duplicate top-level keys, and the
failure is silent twice over: PyYAML's `safe_load` keeps only the last
occurrence of a duplicate key rather than raising, and Hermes then comes up on
whichever half survived without saying anything. The symptom is an agent that
still holds a shell.

So the merge is done structurally, key by key, rather than textually.

## What it does

- Refuses to touch a file that *already* has duplicate top-level keys, naming
  them, because "last one wins" would quietly discard the other block. Pass
  `--force` to merge anyway, which deduplicates by keeping the last.
- Deep-merges: nested mappings are merged key by key, so `agent:` keeps the
  model settings Hermes wrote and simply gains `disabled_toolsets`. Anything
  that is not a mapping is replaced outright, so `disabled_toolsets` is the
  list in the repo rather than a union with whatever was there.
- Backs the original up beside itself before writing.
- Preserves owner, group and mode. The live file belongs to `hermes` and this
  is usually run as root; writing a root-owned config would leave Hermes
  unable to rewrite its own settings later.
- Re-reads and re-parses the result, and checks the two keys that are the
  entire point actually arrived.

Dry run by default. Nothing is written without `--apply`.

## What it costs

Comments in the live file are lost, because this round-trips through
`yaml.safe_load`. That is an accepted trade: the reasoning lives in
`deploy/hermes-config.yaml`, which is the source of truth and is in git, and
Hermes regenerates its own defaults' comments on upgrade. The original is
backed up regardless.

## Usage

    /opt/mudhorn/.venv/bin/python /opt/mudhorn/deploy/merge-hermes-config.py
    /opt/mudhorn/.venv/bin/python /opt/mudhorn/deploy/merge-hermes-config.py --apply

Then restart the Hermes gateway and **ask the agent to run `ls`**. It should
say it has no tool for that. `/tools` and the startup banner are display-only
and will still list `terminal`; see the note in `deploy/hermes-config.yaml`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SOURCE = Path(__file__).resolve().parent / "hermes-config.yaml"
DEFAULT_TARGET = Path("/home/hermes/.hermes/config.yaml")

# The two the whole exercise is for. If a merge reports success without these
# present, it did not do the thing it was run to do.
REQUIRED_AFTER_MERGE = (("agent", "disabled_toolsets"), ("skills", "disabled"))


def duplicate_top_level_keys(text: str) -> list[str]:
    """Top-level keys that appear more than once.

    `yaml.safe_load` cannot answer this: it builds a dict, so a duplicate is
    already gone by the time you hold the result. `yaml.compose` stops at the
    node tree, where both occurrences are still present.
    """
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return []
    if not isinstance(node, yaml.MappingNode):
        return []

    seen: set[str] = set()
    duplicated: list[str] = []
    for key_node, _ in node.value:
        key = getattr(key_node, "value", None)
        if not isinstance(key, str):
            continue
        if key in seen and key not in duplicated:
            duplicated.append(key)
        seen.add(key)
    return duplicated


def merge(base: Any, overlay: Any) -> Any:
    """Recursive mapping merge. Overlay wins at every leaf.

    Lists are replaced rather than concatenated, deliberately. A union would
    make `disabled_toolsets` impossible to shrink: removing an entry from the
    repo file would leave it in place on the box, and the drift would only show
    up as a tool the agent still has.
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay

    merged = dict(base)
    for key, value in overlay.items():
        merged[key] = merge(base.get(key), value) if key in base else value
    return merged


def changes(base: Any, overlay: Any, prefix: str = "") -> list[str]:
    """Human-readable summary of what a merge would do.

    Reports paths and verbs, never values. The live config holds model
    credentials, and a diff that prints values is one screenshot away from
    leaking them — the same reasoning as the Settings page reporting a
    credential as configured rather than rendering it.
    """
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return []

    lines: list[str] = []
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if key not in base:
            lines.append(f"  + {path}{_shape(value)}")
        elif isinstance(value, dict) and isinstance(base[key], dict):
            lines.extend(changes(base[key], value, f"{path}."))
        elif base[key] != value:
            lines.append(f"  ~ {path}{_shape(value)}")
    return lines


def _shape(value: Any) -> str:
    if isinstance(value, list):
        return f" ({len(value)} entries)"
    if isinstance(value, dict):
        return f" ({len(value)} keys)"
    return ""


def _write_preserving_ownership(target: Path, text: str) -> None:
    """Replace `target` atomically, keeping its owner, group and mode.

    This is normally run as root against a file owned by `hermes`. A plain
    write would hand the config to root, and Hermes would then fail to save
    its own settings — quietly, at some later point, for reasons nobody would
    connect back to this script.
    """
    info = target.stat()
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".hermes-merge-")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        if os.geteuid() == 0:
            os.chown(temp, info.st_uid, info.st_gid)
        os.chmod(temp, info.st_mode & 0o7777)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--apply", action="store_true", help="write the merge (default is a dry run)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="merge even if the target already has duplicate top-level keys",
    )
    args = parser.parse_args(argv)

    for path in (args.source, args.target):
        if not path.is_file():
            print(f"not found: {path}", file=sys.stderr)
            return 2

    target_text = args.target.read_text(encoding="utf-8")
    duplicates = duplicate_top_level_keys(target_text)
    if duplicates and not args.force:
        print(
            f"{args.target} already has duplicate top-level keys: "
            f"{', '.join(duplicates)}\n"
            "That file is invalid YAML and Hermes is running on whichever half "
            "survived. Merging would keep only the last of each, discarding the "
            "other. Fix it by hand, or re-run with --force to accept that.",
            file=sys.stderr,
        )
        return 3

    live = yaml.safe_load(target_text) or {}
    source = yaml.safe_load(args.source.read_text(encoding="utf-8")) or {}
    if not isinstance(live, dict) or not isinstance(source, dict):
        print("both files must be YAML mappings", file=sys.stderr)
        return 2

    merged = merge(live, source)
    summary = changes(live, source)

    print(f"source: {args.source}")
    print(f"target: {args.target}")
    if duplicates:
        print(f"WARNING: deduplicating existing duplicate keys: {', '.join(duplicates)}")
    if not summary:
        print("no changes: the live config already matches the repo config")
    else:
        print(f"{len(summary)} change(s):")
        print("\n".join(summary))

    if not args.apply:
        print("\ndry run. Re-run with --apply to write.")
        return 0

    if summary or duplicates:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = args.target.with_name(f"{args.target.name}.bak-{stamp}")
        shutil.copy2(args.target, backup)
        print(f"\nbacked up to {backup}")

        header = (
            "# Merged by deploy/merge-hermes-config.py. The reasoning for every\n"
            "# setting below lives in deploy/hermes-config.yaml, which is the\n"
            "# source of truth. Comments are lost on merge; that file has them.\n"
        )
        _write_preserving_ownership(
            args.target,
            header + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )

    # Read it back off disk rather than trusting what we just built. A file
    # that parses in memory and not on disk is the failure this is here to
    # catch.
    reloaded = yaml.safe_load(args.target.read_text(encoding="utf-8"))
    if not isinstance(reloaded, dict):
        print("FAILED: the merged file did not parse as a mapping", file=sys.stderr)
        return 1

    for section, key in REQUIRED_AFTER_MERGE:
        block = reloaded.get(section)
        if not isinstance(block, dict) or key not in block:
            print(f"FAILED: {section}.{key} is missing after the merge", file=sys.stderr)
            return 1

    toolsets = reloaded["agent"]["disabled_toolsets"]
    skills = reloaded["skills"]["disabled"]
    print(
        f"ok: parses, {len(toolsets)} disabled toolsets, {len(skills)} disabled skills"
    )
    print(
        "Restart the gateway, then ASK THE AGENT to run `ls`. It should have no "
        "tool for it. /tools still lists terminal and that is display only."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
