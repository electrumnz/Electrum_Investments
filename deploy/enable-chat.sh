#!/usr/bin/env bash
#
# Turn the dashboard chat panel on, or off again.
#
#     sudo /opt/mudhorn/deploy/enable-chat.sh          # on
#     sudo /opt/mudhorn/deploy/enable-chat.sh --off    # back off
#     sudo /opt/mudhorn/deploy/enable-chat.sh --status  # what is set now
#
# ## Why this is a script and not four commands in the README
#
# It was four commands, and one of them writes a sudoers file. A malformed
# sudoers file locks the box out of `sudo` entirely, and the realistic moment
# somebody runs this is from a phone, in a web console, one-handed. That is
# exactly when a long paste loses a line.
#
# So the risky step validates into a temporary file with `visudo -c` and is
# only installed once it parses, every step is idempotent, and running it twice
# changes nothing the second time.
#
# ## What it changes
#
#   1. Installs /etc/sudoers.d/mudhorn-chat, which lets the web process run ONE
#      root-owned wrapper as `hermes`. Not the Hermes binary — see run-chat.sh
#      for why that distinction carries weight.
#   2. Adds DASHBOARD_CHAT_TOKEN to .env if it is not already set.
#   3. Reinstalls mudhorn-web.service, which ships WITHOUT NoNewPrivileges and
#      RestrictSUIDSGID because both block sudo.
#
# `--off` reverses 1 and 2 and leaves the unit alone; without the token the
# panel is hidden and POST /chat returns 404, so the sudo grant is inert.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
SUDOERS=/etc/sudoers.d/mudhorn-chat
ENV_FILE="$APP_DIR/.env"
WRAPPER="$APP_DIR/deploy/run-chat.sh"
RULE="mudhorn ALL=(hermes) NOPASSWD: $WRAPPER"

die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

mode="${1:-on}"

# ------------------------------------------------------------------- status

if [[ "$mode" == "--status" ]]; then
  printf '\nChat configuration\n'
  [[ -f "$SUDOERS" ]] && say "sudoers rule: installed" || say "sudoers rule: ABSENT"
  grep -q '^DASHBOARD_CHAT_TOKEN=.\+' "$ENV_FILE" 2>/dev/null \
    && say "token: set" || say "token: NOT set (panel hidden, POST /chat = 404)"
  if systemctl show mudhorn-web -p NoNewPrivileges --value | grep -q yes; then
    say "unit: NoNewPrivileges=yes — sudo is blocked, chat cannot work"
  else
    say "unit: NoNewPrivileges=no — sudo permitted"
  fi
  exit 0
fi

# ---------------------------------------------------------------------- off

if [[ "$mode" == "--off" ]]; then
  printf '\nTurning chat off\n'
  rm -f "$SUDOERS" && say "removed $SUDOERS"
  if grep -q '^DASHBOARD_CHAT_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    sed -i 's/^DASHBOARD_CHAT_TOKEN=.*/DASHBOARD_CHAT_TOKEN=/' "$ENV_FILE"
    say "cleared DASHBOARD_CHAT_TOKEN"
  fi
  systemctl restart mudhorn-web
  say "restarted mudhorn-web"
  printf '\nDone. The panel is hidden and POST /chat returns 404.\n'
  printf 'The unit still permits sudo; to close that too, restore\n'
  printf 'NoNewPrivileges=true and RestrictSUIDSGID=true in\n'
  printf '%s/deploy/systemd/mudhorn-web.service and re-run bootstrap.sh.\n' "$APP_DIR"
  exit 0
fi

[[ "$mode" == "on" ]] || die "unknown option: $mode (use --off or --status)"

# ----------------------------------------------------------------------- on

printf '\nTurning chat on\n'

[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found"

# Runs bootstrap itself rather than telling you to. Dying with "run
# bootstrap.sh first" is one more thing to type, and the realistic place this
# runs is a phone keyboard in a web console where every character costs.
# bootstrap.sh is idempotent, so calling it here is free when it is not needed.
if [[ ! -x "$WRAPPER" ]]; then
  say "wrapper missing, running bootstrap.sh"
  "$APP_DIR/deploy/bootstrap.sh" >/dev/null || die "bootstrap.sh failed"
fi
[[ -x "$WRAPPER" ]] || die "$WRAPPER still missing after bootstrap"

# The wrapper is named in a sudoers rule, so if `mudhorn` can write it the rule
# becomes arbitrary code execution as `hermes`. Checked rather than assumed.
owner="$(stat -c '%U:%a' "$WRAPPER")"
[[ "$owner" == root:755 || "$owner" == root:750 ]] \
  || die "$WRAPPER is $owner, expected root:755 — run ./deploy/bootstrap.sh"

id hermes >/dev/null 2>&1 || die "no 'hermes' user — see docs/HERMES_SETUP.md"

# 1. The sudoers rule, validated before it is anywhere sudo will read it.
if [[ -f "$SUDOERS" ]] && grep -qF "$RULE" "$SUDOERS"; then
  say "sudoers rule already present"
else
  tmp="$(mktemp)"
  printf '%s\n' "$RULE" >"$tmp"
  visudo -c -f "$tmp" >/dev/null || { rm -f "$tmp"; die "the sudoers rule did not parse; nothing installed"; }
  install -m 440 -o root -g root "$tmp" "$SUDOERS"
  rm -f "$tmp"
  say "installed $SUDOERS"
fi
visudo -c >/dev/null || die "sudoers is now invalid — remove $SUDOERS immediately"

# 2. The token. Idempotent: an existing non-empty value is left alone, so
#    re-running does not invalidate a session or append a second line.
if grep -q '^DASHBOARD_CHAT_TOKEN=.\+' "$ENV_FILE"; then
  say "DASHBOARD_CHAT_TOKEN already set, leaving it"
else
  token="$(openssl rand -hex 32)"
  if grep -q '^DASHBOARD_CHAT_TOKEN=' "$ENV_FILE"; then
    sed -i "s|^DASHBOARD_CHAT_TOKEN=.*|DASHBOARD_CHAT_TOKEN=$token|" "$ENV_FILE"
  else
    printf '\nDASHBOARD_CHAT_TOKEN=%s\n' "$token" >>"$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
  say "generated DASHBOARD_CHAT_TOKEN"
fi

# 3. The unit, which must not carry the two settings that block sudo.
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl restart mudhorn-web
say "reinstalled and restarted mudhorn-web"

sleep 2

# Proof rather than hope. Each of these has been a real failure tonight.
if systemctl show mudhorn-web -p NoNewPrivileges --value | grep -q yes; then
  die "mudhorn-web still has NoNewPrivileges=yes, so sudo is blocked and chat cannot work"
fi
say "unit permits sudo"

# Asks the RUNNING SERVICE to do it, over its own HTTP endpoint, rather than
# running sudo from this shell.
#
# That distinction is the whole value of this check. A `sudo -u mudhorn sudo -u
# hermes` from a console shell runs in the console's mount namespace, where
# everything is visible — it passed cleanly while the real thing was failing on
# `cd /home/hermes: Permission denied`, because systemd's sandboxing is a
# namespace that every child of the service inherits and sudo does not escape.
# A check that cannot see the condition it is meant to catch is worse than no
# check, because it is believed.
#
# Going through the endpoint means the service performs the sudo itself, inside
# its own namespace, with its own unit settings. If it works here it works from
# a browser.
say "asking the running service to talk to Hermes (may take a minute)"

password="$(grep -oP '^DASHBOARD_PASSWORD=\K.*' "$ENV_FILE" 2>/dev/null || true)"
chat_token="$(grep -oP '^DASHBOARD_CHAT_TOKEN=\K.*' "$ENV_FILE" 2>/dev/null || true)"
jar="$(mktemp)"
body="$(mktemp)"
trap 'rm -f "$jar" "$body"' EXIT

if [[ -n "$password" ]]; then
  curl -sS --max-time 20 -c "$jar" -o /dev/null \
    -X POST -d "password=$password" http://127.0.0.1:8787/login || true
fi

code="$(curl -sS --max-time 200 -b "$jar" -o "$body" -w '%{http_code}' \
  -X POST -H 'content-type: application/json' \
  -d "{\"token\":\"$chat_token\",\"message\":\"Reply with the single word: READY\"}" \
  http://127.0.0.1:8787/chat || echo 000)"

probe_ok=0
if [[ "$code" != "200" ]]; then
  say "WARNING: POST /chat returned $code rather than 200."
  say "         Check: journalctl -u mudhorn-web -n 30"
elif grep -q '"ok": *true' "$body" || grep -q '"ok":true' "$body"; then
  say "Hermes answered. Chat is working end to end."
  probe_ok=1
else
  say "WARNING: the service reached Hermes and got an error back:"
  sed -n 's/.*"error": *"\([^"]*\)".*/           \1/p' "$body" | head -3
  say "         Chat will show that message rather than an answer."
fi

systemctl is-active --quiet mudhorn-web || die "mudhorn-web is not running"

# **The success block is GUARDED.** This script printed a WARNING and then a
# `Done.` block three lines later, which is this repository's founding failure:
# the reassuring text comes last, so it is the text that gets read.
# `enable-research.sh` was fixed for this and the others were not — the lesson
# had been written down beside one script rather than applied to every script
# that probes itself. A non-zero exit as well as the words, because a guard
# that prints and falls through is the same bug with more output.
#
# This check signs in first, so a non-200 can also mean it could not read
# DASHBOARD_PASSWORD or DASHBOARD_CHAT_TOKEN rather than anything being wrong
# with Hermes. That is said in the message rather than left to be guessed.
if [[ "$probe_ok" != "1" ]]; then
  printf '\nNOT WORKING YET. The grant is installed, but the running service could\n'
  printf 'not get an answer out of Hermes, so the Chat panel will not reply.\n\n'
  printf 'A 401 here can also mean this check could not read DASHBOARD_PASSWORD\n'
  printf 'or DASHBOARD_CHAT_TOKEN out of %s/.env -- a duplicate key is enough.\n' "$APP_DIR"
  printf 'Test the wrapper directly, which skips the web layer entirely:\n'
  printf '    sudo -u hermes %s/deploy/run-chat.sh <<< "reply with just: ok"\n\n' "$APP_DIR"
  printf 'Off again: sudo %s --off\n' "$0"
  exit 1
fi

printf '\nDone. Open the Chat tab and ask it something.\n'
printf 'Off again: sudo %s --off\n' "$0"
