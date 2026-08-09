#!/usr/bin/env bash
#
# Check the Tailscale link and leave a reading for the dashboard to render.
# Run every six hours by mudhorn-tailnet.timer.
#
# This tailnet caps node key lifetime at 90 days and does not allow disabling
# it. When the key lapses the Funnel stops serving and the private address stops
# answering, while the bot carries on trading normally with nothing to say so.
# The point of this script is that the outage is preceded by a warning rather
# than being the first anyone hears of it.
#
# Deliberately thin. Everything worth testing lives in src/bot/tailnet.py, which
# is pure functions over the JSON and is covered by tests/test_tailnet.py. This
# file only knows how to call the CLI and where to put the answer — the same
# split as backup-journal.sh keeping its logic in sqlite3 rather than in bash.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
OUT="${OUT:-$APP_DIR/data/tailnet-status.json}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# `|| true` on both, deliberately. A tailscale binary that is missing, stopped
# or erroring is exactly the condition worth reporting, so it must reach the
# parser as an empty file rather than killing the script under `set -e` and
# leaving the previous reading in place looking current.
tailscale status --json >"$work/status.json" 2>"$work/status.err" || true
tailscale serve status --json >"$work/serve.json" 2>/dev/null || true

if [[ ! -s "$work/status.json" ]]; then
  log "tailscale status produced nothing: $(tr -d '\n' <"$work/status.err" | head -c 200)"
fi

# The CLI writes the status file and exits non-zero when a person should act, so
# systemd records a failed unit. That is the backstop for nobody having opened
# the dashboard; the banner there is the primary channel.
"$PYTHON" -m bot.tailnet \
  --status "$work/status.json" \
  --serve "$work/serve.json" \
  --out "$OUT"
