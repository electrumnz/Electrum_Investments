#!/usr/bin/env bash
#
# Run ONE Hermes turn for the dashboard chat panel. Invoked as `hermes` by the
# `mudhorn` web process:
#
#     sudo -n -u hermes -- /opt/mudhorn/deploy/run-chat.sh
#
# The prompt arrives on STDIN. That is the whole point of this file.
#
# ## Why a wrapper rather than sudo straight to the binary
#
# The obvious sudoers rule is:
#
#     mudhorn ALL=(hermes) NOPASSWD: /home/hermes/.local/bin/hermes
#
# which permits ANY arguments — including `--yolo`, which disables the approval
# system the deployment leans on, and any future flag a Hermes release adds.
# The web process takes its input from whoever is signed in to a dashboard that
# now answers on the public internet, so "any arguments" is not a shape worth
# accepting.
#
# Here the flags are fixed in a root-owned file and the untrusted part travels
# on stdin, where it cannot be read as a flag no matter what it contains. The
# sudoers rule then names this script instead:
#
#     mudhorn ALL=(hermes) NOPASSWD: /opt/mudhorn/deploy/run-chat.sh
#
# Same pattern, same reasoning and the same ownership requirement as
# run-mcp.sh: this must stay root-owned and not writable by `mudhorn`, or the
# rule becomes a way to run arbitrary code as `hermes`. `bootstrap.sh` chowns
# deploy/ to root:root for exactly that reason.
#
# ## A second, quieter benefit
#
# It lives under /opt/mudhorn, which the web process can see. The Hermes binary
# lives under /home/hermes, which is 0700 and which the web unit also hides with
# ProtectHome — so the availability check could not stat it, and `Path.exists()`
# raises rather than returning False on that. That was a 500 on the Chat page.
# Checking for THIS file instead answers the same question honestly, and lets
# ProtectHome stay switched on.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/home/hermes}"
HERMES_BIN="${HERMES_BIN:-$HERMES_HOME/.local/bin/hermes}"

# sudo does not reset HOME by default on every distribution, and Hermes keeps
# its config, its memory and its MCP registry under $HOME. Getting this wrong
# yields an agent that starts fine and has none of its tools — the same silent
# blindness run-mcp.sh exists to prevent.
export HOME="$HERMES_HOME"
cd "$HERMES_HOME"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes is not installed at $HERMES_BIN. See docs/HERMES_SETUP.md." >&2
  exit 127
fi

# The prompt is data, never argv. Read it whole; `-z` takes a single prompt and
# returns the final response on stdout with nothing else.
prompt="$(cat)"

if [[ -z "${prompt//[[:space:]]/}" ]]; then
  echo "empty prompt" >&2
  exit 2
fi

exec "$HERMES_BIN" -z "$prompt"
