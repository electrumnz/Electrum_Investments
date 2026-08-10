#!/usr/bin/env bash
#
# Let the settings agent actually change settings, or take it back.
#
#     sudo /opt/mudhorn/deploy/enable-forge.sh           # on
#     sudo /opt/mudhorn/deploy/enable-forge.sh --off     # back off
#     sudo /opt/mudhorn/deploy/enable-forge.sh --status  # what is set now
#
# ## What this grants, stated plainly before you run it
#
# One sudoers rule, letting the web process run ONE root-owned wrapper as root:
#
#     mudhorn ALL=(root) NOPASSWD: /opt/mudhorn/deploy/apply-settings.sh
#
# That wrapper takes no arguments, reads `apply <id>` or `revert <id>` on stdin,
# and runs one fixed command against one fixed file. The effect is that the
# Armorer can move a limit in config/rules.yaml — a known key, to a number, only
# if the whole file still loads — instead of recording a request for a person to
# apply at a shell.
#
# **This is the only sudo grant here that runs upward.** run-chat.sh and
# run-dream.sh go from `mudhorn` down to `hermes`, an account with no
# credentials; this one goes to root. It is separate from enable-chat.sh for
# that reason and must not be folded into it: turning the chat panel on should
# not quietly hand the service account a way to edit its own risk limits.
#
# ## Why a script and not two commands in the README
#
# One of them writes a sudoers file, and a malformed sudoers file locks the box
# out of `sudo` entirely. The realistic moment somebody runs this is from a
# phone, in a web console, one-handed — exactly when a long paste loses a line.
# So the rule is validated into a temporary file with `visudo -c` and installed
# only once it parses, and every step is idempotent.
#
# `--off` removes the rule. The wrapper stays on disk and becomes inert: the
# settings agent reports that it cannot write the file and goes back to
# recording a request, which is the fallback rather than a failure.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
APP_USER="${APP_USER:-mudhorn}"
SUDOERS=/etc/sudoers.d/mudhorn-forge
WRAPPER="$APP_DIR/deploy/apply-settings.sh"
RULE="$APP_USER ALL=(root) NOPASSWD: $WRAPPER"

die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

mode="${1:-on}"

# ------------------------------------------------------------------- status

if [[ "$mode" == "--status" ]]; then
  printf '\nForge configuration\n'
  if [[ -f "$SUDOERS" ]]; then
    say "sudoers rule: installed — the settings agent CAN change a limit"
  else
    say "sudoers rule: ABSENT — the settings agent records a request only"
  fi
  [[ -x "$WRAPPER" ]] && say "wrapper: $WRAPPER" || say "wrapper: MISSING (run bootstrap.sh)"
  say "config owner: $(stat -c '%U' "$APP_DIR/config" 2>/dev/null || echo unknown) (must be root)"
  exit 0
fi

# ---------------------------------------------------------------------- off

if [[ "$mode" == "--off" ]]; then
  printf '\nTurning the forge off\n'
  rm -f "$SUDOERS" && say "removed $SUDOERS"
  visudo -c >/dev/null || die "sudoers is now invalid — inspect /etc/sudoers.d"
  printf '\nDone. The agent still argues and still records; it can no longer\n'
  printf 'apply. Pending requests: sudo -u %s %s/.venv/bin/electrum-bot settings-apply\n' \
    "$APP_USER" "$APP_DIR"
  exit 0
fi

[[ "$mode" == "on" ]] || die "unknown option: $mode (use --off or --status)"

# ----------------------------------------------------------------------- on

printf '\nTurning the forge on\n'

# bootstrap.sh is idempotent, so calling it here is free when it is not needed,
# and dying with "run bootstrap first" is one more thing to type on a phone.
if [[ ! -x "$WRAPPER" ]]; then
  say "wrapper missing, running bootstrap.sh"
  "$APP_DIR/deploy/bootstrap.sh" >/dev/null || die "bootstrap.sh failed"
fi
[[ -x "$WRAPPER" ]] || die "$WRAPPER still missing after bootstrap"

# The wrapper is named in a sudoers rule granting ROOT, so if `mudhorn` can
# write it the rule is arbitrary code execution as root. Checked, never assumed.
owner="$(stat -c '%U:%a' "$WRAPPER")"
[[ "$owner" == root:755 || "$owner" == root:750 ]] \
  || die "$WRAPPER is $owner, expected root:755 — run ./deploy/bootstrap.sh"

# Same check on the directory holding it: a deploy/ the service account can
# write is a deploy/ where the wrapper can be replaced.
dir_owner="$(stat -c '%U' "$APP_DIR/deploy")"
[[ "$dir_owner" == root ]] || die "$APP_DIR/deploy is owned by $dir_owner, expected root"

# And on config/ itself. If the service account already owns it, this rule is
# pointless AND the safety property it is built around is already gone, which is
# a finding rather than something to work around.
cfg_owner="$(stat -c '%U' "$APP_DIR/config")"
[[ "$cfg_owner" == root ]] \
  || die "$APP_DIR/config is owned by $cfg_owner, expected root — run ./deploy/bootstrap.sh"

id -u "$APP_USER" >/dev/null 2>&1 || die "no '$APP_USER' user — run ./deploy/bootstrap.sh"

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

# The unit has to permit sudo at all, and this is checked BEFORE the wrapper is
# exercised, because the check below cannot see it. `enable-chat.sh` learned
# that the expensive way: systemd sandboxing is a mount namespace every child
# inherits and sudo does not escape one, so a `sudo` run from a console shell
# passes cheerfully while the service itself is refused. The same two settings
# that block the chat panel block this.
if systemctl show mudhorn-web -p NoNewPrivileges --value 2>/dev/null | grep -q yes; then
  say "WARNING: mudhorn-web has NoNewPrivileges=yes, so the web process cannot"
  say "         sudo at all. The rule is installed and the forge will still"
  say "         report that it cannot write the file. Run enable-chat.sh, which"
  say "         reinstalls the unit without it."
fi

# Proof rather than hope, and it proves the grant without changing a limit: an
# id of 0 is never a real request, so this exercises sudo, the wrapper, the
# stdin parsing and the CLI, and stops at "No settings request 0."
#
# It runs from THIS shell rather than through the service, unlike the chat
# check, and that limit is stated rather than papered over: there is no endpoint
# that applies a change without changing one. What it proves is that the rule
# parses, the wrapper runs and the CLI answers. Whether the service can reach it
# is the check above.
say "checking the grant end to end"
out="$(printf 'apply 0\n' | sudo -n -u "$APP_USER" -- sudo -n -u root -- "$WRAPPER" 2>&1 || true)"
if grep -q "No settings request 0" <<<"$out"; then
  say "the wrapper answered — the forge is live"
else
  say "WARNING: the wrapper did not answer as expected:"
  printf '           %s\n' "$out" | head -5
  say "         The agent will report that it cannot write the file."
fi

printf '\nDone. The Armorer applies her own changes now.\n'
printf 'A loosening still waits for the operator to confirm the consequence.\n'
printf 'Off again: sudo %s --off\n' "$0"
