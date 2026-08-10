#!/usr/bin/env bash
#
# Run ONE Hermes turn for the DREAMER panel on /dreaming. Invoked as `hermes`
# by the `mudhorn` web process:
#
#     sudo -n -u hermes -- /opt/mudhorn/deploy/run-dream.sh
#
# The prompt arrives on STDIN, exactly as in run-chat.sh and for exactly the
# same reason: the sudoers rule names this file with no arguments, so nothing a
# signed-in user types can be read as a flag.
#
# ## Why this exists as a second file rather than a flag on run-chat.sh
#
# Two reasons, and the second is the important one.
#
# **Hermes holds one soul per instance.** It loads `SOUL.md` only from
# `$HERMES_HOME` — not the working directory, no CLI flag, no environment
# variable to point at another file. Two characters on one instance is why the
# soul is prepended to the prompt on stdin rather than installed; a second
# HERMES_HOME lets the dreamer have a real one, and its own memory, so Grogu's
# speculation does not bleed into Yoda's answers about the account.
#
# **The dreamer should not be able to reach the broker at all.** The account
# agent's Hermes registers this repo's MCP server, which exposes `place_order`.
# Sharing that instance means the only thing stopping a speculative agent
# placing an order is the sentence in souls/grogu.md telling it not to — prose
# where this repository uses a gate everywhere else. `RiskGate.evaluate` still
# runs on every order path, so the operator's four rules hold either way, but
# "it has no broker tool" and "it has one and was asked nicely" are different
# claims and only one of them is structural.
#
# So this instance's registry must NOT contain the bot's MCP server. That is a
# deployment step, not something this script can enforce, and the Dreaming page
# reports which instance it is actually talking to rather than assuming.
#
# ## Setting it up
#
#   sudo -u hermes env HERMES_HOME=/home/hermes/dreamer hermes   # first run
#   # then, as root:
#   #   - copy souls/grogu.md to /home/hermes/dreamer/SOUL.md
#   #   - ensure /home/hermes/dreamer/config.yaml has NO mcp_servers entry
#   #     pointing at run-mcp.sh
#   #   - add to /etc/sudoers.d/mudhorn-chat:
#   #       mudhorn ALL=(hermes) NOPASSWD: /opt/mudhorn/deploy/run-dream.sh
#
# Same ownership requirement as run-chat.sh and run-mcp.sh: root-owned, not
# writable by `mudhorn`, or the sudoers rule becomes arbitrary code execution as
# `hermes`. `bootstrap.sh` chowns deploy/ to root:root for that reason.

set -euo pipefail

# The whole point of the file: a different home, so a different soul, a
# different memory and a different MCP registry.
HERMES_HOME="${HERMES_DREAM_HOME:-/home/hermes/dreamer}"
HERMES_BIN="${HERMES_BIN:-/home/hermes/.local/bin/hermes}"

# sudo does not reset HOME on every distribution, and Hermes keeps its config,
# memory and MCP registry under $HOME. Getting this wrong yields an agent that
# starts fine with none of its own settings — the silent blindness run-mcp.sh
# exists to prevent, in a different costume.
export HOME="$HERMES_HOME"
cd "$HERMES_HOME"

if [[ ! -x "$HERMES_BIN" ]]; then
  echo "Hermes is not installed at $HERMES_BIN. See docs/HERMES_SETUP.md." >&2
  exit 127
fi

prompt="$(cat)"

if [[ -z "${prompt//[[:space:]]/}" ]]; then
  echo "empty prompt" >&2
  exit 2
fi

exec "$HERMES_BIN" -z "$prompt"
