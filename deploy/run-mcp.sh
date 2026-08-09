#!/usr/bin/env bash
#
# Launch the MCP server with the working directory it needs.
#
# `src/bot/config.py` sets `env_file=".env"` — a relative path — so credentials
# are resolved against the process's working directory. Hermes spawns MCP
# servers from its own home, which means a bare `electrum-bot-mcp` looks for
# `/home/hermes/.env`, fails, and the server dies during startup. Hermes reports
# that as a stdio server stuck on "connecting", with no traceback anywhere the
# operator will look — and the agent then answers questions about the account
# with no idea it is missing every broker tool it should have.
#
# That silent-blindness is the reason this wrapper exists rather than a note in
# the docs telling people to be careful.
#
# Referenced from /etc/sudoers.d/hermes-mcp, so its path is load-bearing:
#
#     hermes ALL=(mudhorn) NOPASSWD: /opt/mudhorn/deploy/run-mcp.sh
#
# It must stay root-owned and not writable by `hermes`, or the sudoers rule
# becomes a way to run arbitrary code as `mudhorn`. `bootstrap.sh` chowns
# deploy/ to root:root for exactly this reason.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"

cd "$APP_DIR"

# exec, not a subshell: Hermes talks to this process over stdio and needs the
# server to own the file descriptors directly.
exec "$APP_DIR/.venv/bin/electrum-bot-mcp" "$@"
