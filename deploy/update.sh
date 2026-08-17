#!/usr/bin/env bash
#
# Pull, provision and restart — with every step CHECKED rather than assumed.
#
#   sudo /opt/mudhorn/deploy/update.sh
#
# ## Why this exists
#
# The runbook used to be four commands, and on 13 Aug 2026 they were pasted as
# one block. The `git pull` ABORTED — two wrappers had been hand-edited on the
# box — and everything after it ran against the old checkout: `bootstrap.sh`
# printed "Provisioned." and a numbered list of next steps, the services
# restarted on the old code, and the verification step afterwards reproduced the
# exact bug the pull was meant to fix, under a banner claiming the check had run.
#
# **Every line of output was true and the deploy had not happened.** The abort
# had scrolled off the top under a wall of successful-looking output.
#
# Two things follow, and this script is both of them.
#
# **A step that can silently not happen must be asserted, not read.** The commit
# is recorded before and after and compared. If it did not move and the remote
# had something to give, that is a hard failure here rather than a line
# somebody was supposed to notice.
#
# **The recovery commands lie too, so they are not offered casually.** `git
# diff` shows UNSTAGED changes only — the edits that day were staged, so it
# printed nothing while the pull kept refusing. `git checkout -- <file>`
# restores from the INDEX, so it faithfully rewrote the modified version back
# and reported success. Both looked like they had worked. This script therefore
# uses `git stash push -u`, which does not care which side the change is on and
# is recoverable, and it only does so when asked with --stash.
#
# ## What it deliberately does NOT do
#
# It does not decide anything for you when the tree is dirty. A hand-edit on a
# box is a legitimate thing to be doing, and a script that silently discarded it
# would be worse than the problem it fixes. Dirty without --stash is a refusal
# WITH the diff printed, which is the one case where stopping is right: nothing
# has happened yet, so nothing is half-done.
#
# It does not touch `.env`, `config/rules.yaml`, `data/` or `audit/`.
#
# It does not start anything that was not already running. `--execute`, the
# dream timer and the confer timer are switched on by a person who decided to,
# and a deploy is not that decision.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
STASH=0
SKIP_VERIFY=0

usage() {
  cat >&2 <<EOF
usage: sudo $0 [--stash] [--skip-verify]

  --stash         stash local changes (recoverable: git stash list) and continue.
                  Without it, a dirty tree is a refusal with the diff printed.
  --skip-verify   do not exercise the deployed wrappers afterwards.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stash) STASH=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "No git checkout at $APP_DIR." >&2
  exit 1
fi

# `safe.directory`: this runs as root over a tree git may consider another
# user's. A refusal there must cost the READING, never the deploy.
GIT=(git -c safe.directory="$APP_DIR" -C "$APP_DIR" --no-pager)

step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\n*** %s\n' "$1" >&2; exit 1; }

# ------------------------------------------------------------------ 1. before

step "Before"
BEFORE="$("${GIT[@]}" rev-parse HEAD)"
BRANCH="$("${GIT[@]}" rev-parse --abbrev-ref HEAD)"
echo "    $BRANCH at $("${GIT[@]}" log -1 --format='%h %s')"

# ------------------------------------------------------------------ 2. dirty

DIRTY="$("${GIT[@]}" status --porcelain)"
if [[ -n "$DIRTY" ]]; then
  step "Uncommitted changes"
  echo "$DIRTY" | sed 's/^/    /'
  if [[ $STASH -eq 0 ]]; then
    echo ""
    echo "    Staged changes print NOTHING under 'git diff' and survive"
    echo "    'git checkout -- <file>', which restores from the index. Both"
    echo "    commands report success while the file is untouched, which is how"
    echo "    a deploy silently did not happen on 13 Aug 2026."
    echo ""
    "${GIT[@]}" diff --stat HEAD | sed 's/^/    /' || true
    fail "Dirty tree. Read the above, then re-run with --stash to set it aside
    (recoverable with 'sudo git -C $APP_DIR stash list'), or commit it."
  fi
  echo "    stashing"
  "${GIT[@]}" stash push -u -m "update.sh $(date -u +%Y-%m-%dT%H:%M:%SZ) before $BEFORE"
  echo "    recover with: sudo git -C $APP_DIR stash list"
fi

# ------------------------------------------------------------------- 3. fetch

step "Fetch"
"${GIT[@]}" fetch --prune origin
UPSTREAM="$("${GIT[@]}" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
[[ -n "$UPSTREAM" ]] || fail "$BRANCH tracks no upstream. Set one, or check out a branch that does."
TARGET="$("${GIT[@]}" rev-parse "$UPSTREAM")"
echo "    $UPSTREAM at ${TARGET:0:7}"

# --------------------------------------------------------------------- 4. pull

if [[ "$BEFORE" == "$TARGET" ]]; then
  step "Pull"
  echo "    already at $UPSTREAM, nothing to fetch"
else
  step "Pull"
  # --ff-only, so a diverged branch STOPS here rather than opening a merge in a
  # non-interactive shell. Merging on the box is a decision, not a deploy step.
  "${GIT[@]}" merge --ff-only "$UPSTREAM"
fi

AFTER="$("${GIT[@]}" rev-parse HEAD)"

# **The assertion the old runbook did not have.** A pull that aborted printed
# its error and returned; nothing downstream could tell. Here, a target that
# was not reached is a hard stop before anything is provisioned.
if [[ "$AFTER" != "$TARGET" ]]; then
  fail "HEAD is ${AFTER:0:7} but $UPSTREAM is ${TARGET:0:7}. The pull did not
    land. Nothing has been provisioned or restarted."
fi

if [[ "$BEFORE" == "$AFTER" ]]; then
  echo "    no change (${AFTER:0:7})"
else
  echo "    ${BEFORE:0:7} -> ${AFTER:0:7}"
  "${GIT[@]}" log --oneline "$BEFORE..$AFTER" | sed 's/^/      /'
fi

# ---------------------------------------------------------------- 5. provision

step "Provision"
"$APP_DIR/deploy/bootstrap.sh"

# ------------------------------------------------------------------ 6. restart

step "Restart"
# Only what is already enabled, and only what exists. A deploy must not be the
# thing that starts a service somebody had deliberately stopped.
RESTARTED=()
for unit in mudhorn-bot mudhorn-web; do
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    systemctl restart "$unit"
    RESTARTED+=("$unit")
  else
    echo "    $unit is not enabled, leaving it alone"
  fi
done

# Give systemd a moment to fail a unit that is going to fail immediately —
# otherwise `is-active` catches it mid-start and reports healthy.
sleep 3
for unit in "${RESTARTED[@]}"; do
  if systemctl is-active --quiet "$unit"; then
    echo "    $unit active"
  else
    echo ""
    systemctl status "$unit" --no-pager --lines=20 || true
    fail "$unit did not come back. The code is at ${AFTER:0:7}; the service is not running."
  fi
done

# ------------------------------------------------------------------- 7. verify

if [[ $SKIP_VERIFY -eq 0 && -x "$APP_DIR/deploy/run-chat.sh" ]]; then
  step "Verify the wrapper's model check"
  # **Exercised in a SANDBOX, against the deployed file.** `run-chat.sh` takes
  # its home and its binary from the environment, which is exactly how
  # `tests/test_config.py` drives it — so a mismatched pair can be put in front
  # of the real script without touching /home/hermes at all. Mutating the live
  # `inference.env` to test it would be a deploy step that edits an agent's
  # credentials file, which is not a trade worth making for a check.
  SANDBOX="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$SANDBOX'" EXIT
  mkdir -p "$SANDBOX/.hermes" "$SANDBOX/bin"
  printf '#!/bin/sh\necho "stub hermes answered"\n' > "$SANDBOX/bin/hermes"
  chmod 755 "$SANDBOX/bin/hermes"
  printf 'DO_INFERENCE_KEY=sandbox\nDO_INFERENCE_MODEL=model-a\n' > "$SANDBOX/inference.env"
  printf 'model:\n  default: model-b\n' > "$SANDBOX/.hermes/config.yaml"

  set +e
  OUT="$(echo hi | HERMES_HOME="$SANDBOX" HERMES_BIN="$SANDBOX/bin/hermes" \
        "$APP_DIR/deploy/run-chat.sh" 2>&1)"
  RC=$?
  set -e

  if [[ $RC -ne 78 ]]; then
    echo "$OUT" | sed 's/^/    /'
    fail "run-chat.sh answered a MISMATCHED model instead of refusing (exit $RC,
    wanted 78). The deployed wrapper is announcing a model it has not checked."
  fi
  case "$OUT" in
    *"model-a"*"model-b"*|*"model-b"*"model-a"*) ;;
    *) echo "$OUT" | sed 's/^/    /'
       fail "It refused, but without naming both models. A refusal that does not
    say which two values disagree is most of the way to no message." ;;
  esac
  echo "    refuses a mismatch, exit 78, both values named"
  rm -rf "$SANDBOX"
  trap - EXIT
fi

# ------------------------------------------------------------------ 8. summary

step "Done"
echo "    $BRANCH  ${BEFORE:0:7} -> ${AFTER:0:7}"
if [[ ${#RESTARTED[@]} -gt 0 ]]; then
  echo "    restarted: ${RESTARTED[*]}"
else
  echo "    restarted: nothing (no enabled units)"
fi
if [[ -n "$DIRTY" && $STASH -eq 1 ]]; then
  echo "    stashed local changes: sudo git -C $APP_DIR stash list"
fi
echo ""
echo "    Nothing here switched on --execute, the dream timer or the confer"
echo "    timer. Those are a person's decision, and a deploy is not it."
# **It does not switch them OFF either, and that half was missing.** The
# sentence above is about what this script DID; read alone it invites the
# reading that nothing is running, which is how a session concluded the dream
# timer had never fired while it had in fact run that morning. bootstrap.sh
# reports each timer's real state; this points at the command that answers it
# for somebody reading only this output.
echo ""
echo "    It did not switch anything OFF either. For what is actually running:"
echo "        systemctl list-timers 'mudhorn-*'"
