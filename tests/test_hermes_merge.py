"""The Hermes config merge.

The load-bearing assertions are the ones about silence. A duplicate top-level
key is not a YAML error anyone sees: `safe_load` keeps the last occurrence and
Hermes starts on whatever survived, still holding a shell. So the merge has to
detect that case itself rather than inherit the parser's shrug.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "merge-hermes-config.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("merge_hermes_config", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merger = _load()


SOURCE = """
agent:
  disabled_toolsets:
    - terminal
    - browser
skills:
  disabled:
    - github-auth
approvals:
  mode: manual
"""

LIVE = """
model:
  name: some-model
agent:
  max_turns: 40
  streaming: true
skills:
  auto_load: true
approvals:
  mode: smart
  cron_mode: prompt
"""


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(LIVE, encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "hermes-config.yaml"
    path.write_text(SOURCE, encoding="utf-8")
    return path


# ------------------------------------------------------------ duplicate keys


def test_a_duplicate_top_level_key_is_detected_where_safe_load_hides_it():
    text = "agent:\n  a: 1\nskills:\n  b: 2\nagent:\n  c: 3\n"

    # The parser itself is perfectly happy, which is the entire problem.
    assert yaml.safe_load(text) == {"agent": {"c": 3}, "skills": {"b": 2}}
    assert merger.duplicate_top_level_keys(text) == ["agent"]


def test_a_clean_file_reports_no_duplicates(target: Path):
    assert merger.duplicate_top_level_keys(target.read_text()) == []


def test_merging_refuses_a_file_that_already_has_duplicates(
    source: Path, tmp_path: Path
):
    """This is the state `cat >>` leaves behind, and it must not be papered over."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(LIVE + "\nagent:\n  clobbered: true\n", encoding="utf-8")

    code = merger.main(["--source", str(source), "--target", str(broken), "--apply"])

    assert code == 3
    # Refused means refused: the file is untouched.
    assert "clobbered" in broken.read_text()


def test_force_accepts_the_duplicate_and_deduplicates(source: Path, tmp_path: Path):
    broken = tmp_path / "broken.yaml"
    broken.write_text(LIVE + "\nagent:\n  clobbered: true\n", encoding="utf-8")

    code = merger.main(
        ["--source", str(source), "--target", str(broken), "--apply", "--force"]
    )

    assert code == 0
    assert merger.duplicate_top_level_keys(broken.read_text()) == []


# ------------------------------------------------------------------ the merge


def test_merge_keeps_the_settings_hermes_wrote_alongside_ours():
    """The reason this is not a `cat >>`: `agent:` already has content."""
    merged = merger.merge(yaml.safe_load(LIVE), yaml.safe_load(SOURCE))

    assert merged["agent"]["max_turns"] == 40
    assert merged["agent"]["streaming"] is True
    assert merged["agent"]["disabled_toolsets"] == ["terminal", "browser"]
    assert merged["skills"]["auto_load"] is True
    assert merged["model"]["name"] == "some-model"


def test_our_value_wins_where_both_define_one():
    merged = merger.merge(yaml.safe_load(LIVE), yaml.safe_load(SOURCE))

    assert merged["approvals"]["mode"] == "manual"
    # A key we say nothing about survives untouched.
    assert merged["approvals"]["cron_mode"] == "prompt"


def test_lists_are_replaced_rather_than_unioned():
    """A union would make a toolset impossible to re-enable from the repo.

    Dropping an entry from `disabled_toolsets` would leave it disabled on the
    box, and the drift would only surface as a tool the agent no longer has.
    """
    base = {"agent": {"disabled_toolsets": ["terminal", "cronjob", "spotify"]}}
    overlay = {"agent": {"disabled_toolsets": ["terminal"]}}

    assert merger.merge(base, overlay)["agent"]["disabled_toolsets"] == ["terminal"]


def test_the_change_summary_names_paths_and_never_prints_values():
    """The live config holds model credentials. A diff is one screenshot away."""
    summary = merger.changes(yaml.safe_load(LIVE), yaml.safe_load(SOURCE))
    text = "\n".join(summary)

    assert "+ agent.disabled_toolsets (2 entries)" in text
    assert "~ approvals.mode" in text
    assert "manual" not in text
    assert "terminal" not in text
    # A key that did not change is not mentioned at all.
    assert "cron_mode" not in text


# ------------------------------------------------------------------- writing


def test_a_dry_run_writes_nothing(source: Path, target: Path):
    before = target.read_text()

    assert merger.main(["--source", str(source), "--target", str(target)]) == 0
    assert target.read_text() == before


def test_apply_writes_the_merge_and_backs_up_the_original(source: Path, target: Path):
    code = merger.main(["--source", str(source), "--target", str(target), "--apply"])

    assert code == 0
    written = yaml.safe_load(target.read_text())
    assert written["agent"]["disabled_toolsets"] == ["terminal", "browser"]
    assert written["agent"]["max_turns"] == 40

    backups = list(target.parent.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text())["approvals"]["mode"] == "smart"


def test_apply_preserves_the_file_mode(source: Path, target: Path):
    """The config belongs to hermes and this runs as root."""
    target.chmod(0o600)

    merger.main(["--source", str(source), "--target", str(target), "--apply"])

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_a_second_run_is_a_no_op(source: Path, target: Path):
    merger.main(["--source", str(source), "--target", str(target), "--apply"])
    after_first = target.read_text()

    assert merger.main(["--source", str(source), "--target", str(target), "--apply"]) == 0
    assert target.read_text() == after_first
    # No second backup, because there was nothing to back up.
    assert len(list(target.parent.glob("config.yaml.bak-*"))) == 1


def test_a_merge_that_did_not_deliver_the_two_keys_fails_loudly(
    target: Path, tmp_path: Path
):
    """Reporting success while the agent keeps its shell is the worst outcome."""
    thin = tmp_path / "thin.yaml"
    thin.write_text("approvals:\n  mode: manual\n", encoding="utf-8")

    code = merger.main(["--source", str(thin), "--target", str(target), "--apply"])

    assert code == 1


def test_the_repo_config_parses_and_carries_both_blocks():
    """Guards the file the operator actually merges, not just the merger."""
    config = yaml.safe_load(SCRIPT.parent.joinpath("hermes-config.yaml").read_text())

    assert "terminal" in config["agent"]["disabled_toolsets"]
    assert config["skills"]["disabled"]
    assert config["approvals"]["mode"] == "manual"
    assert config["mcp_servers"]["electrum-bot"]["command"] == "sudo"
