#!/usr/bin/env bash
#
# Restart the decision loop if it has stopped doing work. Run by
# mudhorn-watchdog.timer.
#
# The loop died at 12:04 UTC on 22 Aug 2026 on a transient Alpaca 503 and was
# still dead four days later, because systemd's restart limiter gave up after
# 165 seconds and nothing was allowed to try again. The unit's restart policy is
# fixed separately and is the larger half of that repair; this exists for the
# half a restart policy structurally cannot see — a loop that is `active
# (running)` and silently doing nothing.
#
# Deliberately thin, the same split as check-tailscale.sh. Everything worth
# testing is in src/bot/watchdog.py, which is pure functions over a JobHistory
# and is covered by tests/test_watchdog.py. This file only knows how to ask
# systemd two questions, and how to act on the answer.
#
# ## The exit codes, which are the interface
#
#   0   nothing to do, or a restart was asked for and carried out
#   1   a person should look — see `Action.REPORT` and the four refusals
#   10  (from the Python only) restart wanted; consumed here, never propagated
#
# A successful restart exits 0 on purpose. Self-healing is the feature, and
# exiting non-zero every time one happened would leave `systemctl --failed`
# permanently dirty, which teaches the reader to disregard it — the same
# reasoning that put RECHECK_COMMAND on the tailnet banner.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
UNIT="${UNIT:-mudhorn-bot}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

cd "$APP_DIR"

# `|| true` on both. A unit that does not exist, a systemctl that fails, a dbus
# that is not answering — every one of those must reach the Python as an empty
# string, which `parse_unit_state` reads as UNKNOWN and refuses to act on. Dying
# here under `set -e` would leave the timer failing silently with no verdict at
# all, which is the outage this script exists to shorten wearing a new hat.
state="$(systemctl is-active "$UNIT" 2>/dev/null || true)"

# How long it has been up, so a loop that has merely just started is not
# mistaken for one that has stopped working. Empty means could-not-ask, and the
# Python treats that as unknown rather than as zero seconds.
active_for=""
entered="$(systemctl show "$UNIT" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
if [[ -n "$entered" ]]; then
  if since="$(date -d "$entered" +%s 2>/dev/null)"; then
    active_for="$(( $(date +%s) - since ))"
  fi
fi

args=(--unit-state "$state")
[[ -n "$active_for" ]] && args+=(--active-for-seconds "$active_for")

# The Python decides and writes the restart ledger; it never restarts anything.
# The privileged action stays here, as one visible line in a file a person can
# read, rather than inside a module that is otherwise pure.
set +e
verdict="$("$PYTHON" -m bot.watchdog "${args[@]}" 2>&1)"
rc=$?
set -e

log "$verdict"

case "$rc" in
  0)
    exit 0
    ;;
  10)
    log "restarting $UNIT"
    # `reset-failed` first, because a unit that has exhausted its start limit
    # refuses a plain `start` — which is exactly the state this was written for,
    # and skipping it would make the watchdog silently useless in its own
    # founding case. Harmless on a unit that is merely wedged.
    systemctl reset-failed "$UNIT" || true
    if systemctl restart "$UNIT"; then
      log "restart of $UNIT succeeded"
      exit 0
    fi
    log "RESTART FAILED for $UNIT — a person needs to look"
    exit 1
    ;;
  *)
    # Includes the Python having failed outright. An unexpected code is a
    # person's problem, never an implicit all-clear.
    exit 1
    ;;
esac
