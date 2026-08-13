#!/usr/bin/env bash
#
# Apply — or revert — ONE settings change the Armorer argued. Invoked as root
# by the `mudhorn` web process:
#
#     sudo -n -u root -- /opt/mudhorn/deploy/apply-settings.sh
#
# The request id arrives on STDIN. That is the whole point of this file.
#
# ## Why a wrapper rather than sudo straight to the binary
#
# The obvious sudoers rule is:
#
#     mudhorn ALL=(root) NOPASSWD: /opt/mudhorn/.venv/bin/electrum-bot
#
# which permits ANY arguments — `loop --execute`, every other subcommand this
# CLI has today, and every one a future release adds, run as root, chosen by
# whoever is signed in to a dashboard that may answer on the public internet.
#
# Here the command is fixed in a root-owned file and the untrusted part travels
# on stdin, where it cannot be read as a flag no matter what it contains. The
# sudoers rule then names this script instead:
#
#     mudhorn ALL=(root) NOPASSWD: /opt/mudhorn/deploy/apply-settings.sh
#
# Same pattern, same reasoning and the same ownership requirement as
# run-chat.sh and run-mcp.sh: this must stay root-owned and not writable by
# `mudhorn`, or the rule becomes a way to run arbitrary code as root.
# `bootstrap.sh` chowns deploy/ to root:root for exactly that reason.
#
# ## This escalation runs UPWARD, and that is worth saying out loud
#
# run-chat.sh sudoes DOWNWARD, from `mudhorn` to `hermes`, an account holding no
# credentials. This one goes the other way. So be exact about what it grants:
#
#   - the command is fixed here, not chosen by the caller
#   - the rules file is fixed here, not chosen by the caller
#   - the id is validated against ^(apply|revert) <digits>$ before anything runs
#   - `settings-apply` edits ONE line naming a limit in `settings_agent.limits_for`,
#     to a number, and only if the whole file still loads through `Rules.load`
#     on a staged copy — otherwise it writes nothing
#
# What `mudhorn` gains is the ability to move a known limit to a value the bot
# can start on, recorded in data/settings_requests.db and revertible. What it
# does not gain is writing arbitrary bytes as root, reaching any other file, or
# leaving a config the loop cannot load. config/ stays root-owned and is still
# only ever written by root.
#
# That is a real widening of what the service account can do, and it is the one
# the operator asked for: "the Armorer can access and change trading settings".

set -euo pipefail

# Fixed here rather than inherited. sudo resets the environment by default, but
# this file is the thing the sudoers rule trusts, so it states its own PATH
# instead of depending on a setting somewhere else being right.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

APP_DIR="${APP_DIR:-/opt/mudhorn}"
APP_USER="${APP_USER:-mudhorn}"
BIN="$APP_DIR/.venv/bin/electrum-bot"
RULES="$APP_DIR/config/rules.yaml"
STORE="$APP_DIR/data/settings_requests.db"

if [[ ! -x "$BIN" ]]; then
  echo "electrum-bot is not installed at $BIN. Run deploy/bootstrap.sh." >&2
  exit 127
fi

if [[ ! -f "$RULES" ]]; then
  echo "No rules file at $RULES." >&2
  exit 2
fi

# The id is data, never argv. Read the FIRST line only: a second line is not a
# second request, it is something unexpected, and quietly ignoring the rest of
# an input this file did not expect is how a wrapper stops meaning what its
# comments say.
read -r line || true

# Validated here as well as in Python. Python builds this string from an int so
# it cannot be malformed today, but this script is what the sudoers rule points
# at, and a file that trusts its caller is not a boundary.
if [[ "$line" =~ ^([0-9]{1,9})$ ]]; then
  verb=apply
  id="${BASH_REMATCH[1]}"
elif [[ "$line" =~ ^(apply|revert)[[:space:]]+([0-9]{1,9})$ ]]; then
  verb="${BASH_REMATCH[1]}"
  id="${BASH_REMATCH[2]}"
else
  echo "Expected 'apply <id>' or 'revert <id>' on stdin. Nothing was done." >&2
  exit 2
fi

# The working directory decides where `data/settings_requests.db` resolves to,
# and getting it wrong yields a command that cheerfully creates an EMPTY store
# and then reports the id as unknown — a confident wrong answer about a risk
# limit. Both paths are passed explicitly anyway; the cd is the belt.
cd "$APP_DIR"

set +e
"$BIN" "settings-$verb" "$id" --rules "$RULES" --requests "$STORE"
status=$?
set -e

# Root has just written a database the service account owns, and SQLite writes
# sidecar files beside it. A root-owned journal or WAL left in data/ makes the
# store unwritable by the process that owns it, at the next request rather than
# now — the kind of failure that is discovered long after its cause. Put the
# ownership back.
if id -u "$APP_USER" >/dev/null 2>&1; then
  for suffix in "" "-journal" "-wal" "-shm"; do
    # `if` rather than `[[ ... ]] && chown`: under `set -e` an AND-list whose
    # test is false returns non-zero, and this is the last statement in the
    # loop body, so the script would exit before reporting the CLI's status —
    # the one thing the caller is waiting for.
    if [[ -e "$STORE$suffix" ]]; then
      chown "$APP_USER:$APP_USER" "$STORE$suffix"
    fi
  done
fi

exit "$status"
