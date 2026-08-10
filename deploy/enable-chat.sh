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

[[ -x "$WRAPPER" ]] || die "$WRAPPER is missing or not executable — run ./deploy/bootstrap.sh first"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found"

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

# Exactly what the web process does: mudhorn sudo-ing to hermes to run the
# wrapper. Captured explicitly rather than read from $? inside an elif, where
# an intervening command would silently change it — a verification that
# misreports is worse than no verification.
set +e
sudo -n -u mudhorn sudo -n -u hermes -- "$WRAPPER" </dev/null >/dev/null 2>&1
rc=$?
set -e

case "$rc" in
  # 2 is run-chat.sh refusing an empty prompt, which means it ran as hermes.
  # That is the success case here: we are testing the permission, not Hermes.
  2)   say "wrapper ran as hermes and refused the empty prompt, as designed" ;;
  0)   say "wrapper ran as hermes" ;;
  127) say "WARNING: sudo works but Hermes is not installed at its expected path."
       say "         Chat will report that rather than answering. See docs/HERMES_SETUP.md." ;;
  1)   say "WARNING: sudo refused. The rule may not have taken effect."
       say "         Check: sudo -n -u mudhorn sudo -n -u hermes -- $WRAPPER" ;;
  *)   say "WARNING: unexpected exit $rc from the wrapper. Chat may fail."
       say "         Check: sudo -n -u mudhorn sudo -n -u hermes -- $WRAPPER" ;;
esac

systemctl is-active --quiet mudhorn-web || die "mudhorn-web is not running"

printf '\nDone. Open the Chat tab and ask it something.\n'
printf 'Off again: sudo %s --off\n' "$0"
