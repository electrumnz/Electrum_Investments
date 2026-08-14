#!/usr/bin/env bash
#
# Give the dreamer its own Hermes instance, or take it away again.
#
#     sudo /opt/mudhorn/deploy/enable-dream.sh           # on
#     sudo /opt/mudhorn/deploy/enable-dream.sh --off     # back off
#     sudo /opt/mudhorn/deploy/enable-dream.sh --status  # what is set now
#
# ## What this is for
#
# The account agent's Hermes registers this repo's MCP server, which exposes
# `place_order`. Sharing that instance means the only thing keeping a
# speculative agent away from the broker is the sentence in `souls/grogu.md`
# telling it not to — prose, where this repository uses a structure everywhere
# else. `RiskGate.evaluate` still runs on every order path either way, so the
# operator's four rules hold; but "it has no broker tool" and "it has one and
# was asked nicely" are different claims and only one is worth making.
#
# ## Why this script exists
#
# The same reason `enable-chat.sh` does: one of these steps writes a sudoers
# file, a malformed sudoers file locks the box out of `sudo` entirely, and the
# realistic moment somebody runs this is one-handed in a web console. So the
# risky step validates into a temporary file with `visudo -c` and is installed
# only once it parses, and every step is idempotent.
#
# It is a SEPARATE sudoers file from `mudhorn-chat` on purpose:
# `enable-chat.sh --off` removes that file, and the dreamer's grant has no
# business disappearing because somebody toggled the chat panel.
#
# ## The failure it was written after
#
# Observed on the droplet, 14 Aug 2026. `bootstrap.sh` ships `run-dream.sh` and
# makes it executable on every box whether or not the second instance exists —
# it is inert without a sudoers rule naming it. But the dashboard decided which
# instance to use by asking whether the FILE was there, so on a box with no
# grant and no `/home/hermes/dreamer` it routed Grogu to an instance sudo
# refuses. The panel answered `sudo: a password is required`, and the page
# claimed the dreamer was isolated while it could not run at all.
#
# The dashboard asks `sudo -n -l` now, so an ungranted wrapper falls back to
# the shared instance and the page says so. This script is the other half: the
# deliberate act that makes the isolated arrangement real.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
SUDOERS=/etc/sudoers.d/mudhorn-dream
WRAPPER="$APP_DIR/deploy/run-dream.sh"
RULE="mudhorn ALL=(hermes) NOPASSWD: $WRAPPER"
DREAM_HOME=/home/hermes/dreamer
SOUL_SRC="$APP_DIR/souls/grogu.md"

die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

mode="${1:-on}"

# ------------------------------------------------------------------- status

if [[ "$mode" == "--status" ]]; then
  printf '\nDreamer instance\n'
  [[ -f "$SUDOERS" ]] && say "sudoers rule: installed" || say "sudoers rule: ABSENT"
  [[ -d "$DREAM_HOME" ]] && say "home: $DREAM_HOME" || say "home: ABSENT"
  [[ -f "$DREAM_HOME/SOUL.md" ]] && say "soul: installed" || say "soul: ABSENT"
  [[ -f "$DREAM_HOME/.hermes/config.yaml" ]] \
    && say "config: present" || say "config: ABSENT (run hermes once in that home)"
  if [[ -f "$DREAM_HOME/.hermes/config.yaml" ]] \
     && grep -qE 'run-mcp\.sh|electrum-bot' "$DREAM_HOME/.hermes/config.yaml"; then
    say "MCP: REGISTERED — this instance can reach the broker, which defeats the point"
  else
    say "MCP: not registered (correct)"
  fi
  exit 0
fi

# ---------------------------------------------------------------------- off

if [[ "$mode" == "--off" ]]; then
  printf '\nTurning the dreamer instance off\n'
  rm -f "$SUDOERS" && say "removed $SUDOERS"
  systemctl restart mudhorn-web
  say "restarted mudhorn-web"
  # The home is left alone deliberately. It holds the dreamer's own memory, and
  # deleting an agent's history to withdraw a permission is a far bigger act
  # than the one being asked for. Without the sudoers rule it is unreachable.
  printf '\nDone. Grogu falls back to the shared instance and the page says so.\n'
  printf 'The home at %s is left in place; it is unreachable without the rule.\n' "$DREAM_HOME"
  exit 0
fi

[[ "$mode" == "on" ]] || die "unknown option: $mode (use --off or --status)"

# ----------------------------------------------------------------------- on

printf '\nTurning the dreamer instance on\n'

id hermes >/dev/null 2>&1 || die "no 'hermes' user — see docs/HERMES_SETUP.md"

if [[ ! -x "$WRAPPER" ]]; then
  say "wrapper missing, running bootstrap.sh"
  "$APP_DIR/deploy/bootstrap.sh" >/dev/null || die "bootstrap.sh failed"
fi
[[ -x "$WRAPPER" ]] || die "$WRAPPER still missing after bootstrap"

# Named in a sudoers rule, so if `mudhorn` can write it the rule becomes
# arbitrary code execution as `hermes`. Checked rather than assumed.
owner="$(stat -c '%U:%a' "$WRAPPER")"
[[ "$owner" == root:755 || "$owner" == root:750 ]] \
  || die "$WRAPPER is $owner, expected root:755 — run ./deploy/bootstrap.sh"

# 1. The home. `run-dream.sh` does `cd "$HERMES_HOME"` and dies without it —
#    which is a failure the sudo grant alone would not have fixed.
if [[ -d "$DREAM_HOME" ]]; then
  say "home already exists at $DREAM_HOME"
else
  install -d -m 700 -o hermes -g hermes "$DREAM_HOME"
  say "created $DREAM_HOME"
fi

# 2. The soul. Hermes loads SOUL.md from $HERMES_HOME and nowhere else, which
#    is the whole reason a second home buys a second character.
if [[ -f "$SOUL_SRC" ]]; then
  install -m 600 -o hermes -g hermes "$SOUL_SRC" "$DREAM_HOME/SOUL.md"
  say "installed grogu.md as $DREAM_HOME/SOUL.md"
else
  say "WARNING: $SOUL_SRC not found; the dreamer will run voiceless"
fi

# 3. The credential and the model, copied from the account agent's home.
#
# **Without this the grant makes things WORSE, and it did.** Observed on the
# droplet the first time this script ran: the home, the soul and the sudoers
# rule all installed cleanly, the dashboard duly started routing Grogu to the
# isolated instance — and that instance had no inference credential, so it
# answered "No inference provider configured". Before the grant Grogu worked
# through the fallback; after it he did not. A permission to reach something
# unfinished is worth less than no permission at all.
#
# The same DigitalOcean key as the account agent, deliberately. A second key
# would be a second thing to rotate and a second balance to keep topped up, and
# the isolation this buys is about TOOLS — no MCP server, so no `place_order` —
# not about billing. `run-dream.sh` reads its own home's file, so pointing the
# dreamer at a different model later is a one-line edit here and no code change.
SHARED_ENV=/home/hermes/inference.env
DREAM_ENV="$DREAM_HOME/inference.env"

if [[ -f "$DREAM_ENV" ]]; then
  say "inference.env already present, leaving it"
elif [[ -f "$SHARED_ENV" ]]; then
  install -m 600 -o hermes -g hermes "$SHARED_ENV" "$DREAM_ENV"
  say "copied inference.env from the account agent"
else
  say "WARNING: $SHARED_ENV not found, so the dreamer has no credential."
  say "         It will report 'No inference provider configured'."
fi

# The model, which Hermes reads from its OWN config and not from the
# environment — measured on the chat instance, where the banner named one model
# while Hermes asked for another. `run-dream.sh` refuses to run rather than
# announce a model it has not checked, so a home with a credential and no
# config is still a home that cannot answer.
#
# Written directly rather than by running `hermes model`, which is interactive
# and cannot be driven from a provisioning script. Minimal on purpose: the
# model, the approval mode, and NO `mcp_servers` key — that absence is the
# whole point of this instance and is asserted below.
DREAM_CONFIG_DIR="$DREAM_HOME/.hermes"
DREAM_CONFIG="$DREAM_CONFIG_DIR/config.yaml"
model=""
[[ -f "$DREAM_ENV" ]] && model="$(grep -oP '^DO_INFERENCE_MODEL=\K.*' "$DREAM_ENV" 2>/dev/null || true)"

if [[ -f "$DREAM_CONFIG" ]]; then
  say "config.yaml already present, leaving it"
elif [[ -n "$model" ]]; then
  install -d -m 700 -o hermes -g hermes "$DREAM_CONFIG_DIR"
  tmp_cfg="$(mktemp)"
  cat >"$tmp_cfg" <<YAML
# Written by deploy/enable-dream.sh. The dreamer's own instance.
#
# There is deliberately NO mcp_servers key. This agent speculates; it must not
# be able to reach the broker, and that is a property of the process rather
# than of a sentence in its soul file.
model:
  default: $model
approvals:
  mode: manual
  cron_mode: deny
YAML
  install -m 600 -o hermes -g hermes "$tmp_cfg" "$DREAM_CONFIG"
  rm -f "$tmp_cfg"
  say "wrote $DREAM_CONFIG with model.default=$model"
else
  say "WARNING: no DO_INFERENCE_MODEL to write into a config; the dreamer"
  say "         cannot answer until one is set in $DREAM_ENV."
fi

# 4. The sudoers rule, validated before it is anywhere sudo will read it.
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

# 5. The quarantine, checked rather than trusted. This is the entire argument
#    for a second instance, so it is verified here as well as by the wrapper
#    at run time: a home whose config registers the MCP server is a dreamer
#    with `place_order`, which is worse than no second instance at all because
#    the page would then claim an isolation that is actively false.
CONFIG="$DREAM_HOME/.hermes/config.yaml"
if [[ -f "$CONFIG" ]] && grep -qE 'run-mcp\.sh|electrum-bot' "$CONFIG"; then
  die "$CONFIG registers this repo's MCP server. Remove the mcp_servers entry
       from THIS home — the account agent keeps its own under /home/hermes."
fi

systemctl restart mudhorn-web
say "restarted mudhorn-web"

# **Waits for the port rather than sleeping a fixed two seconds**, and the
# first run of this script is why. `mudhorn-web` serves Server-Sent Events on
# `/live`, an SSE connection never closes on its own, and uvicorn's graceful
# shutdown waited for one — so `systemctl restart` sat in `deactivating` for
# the full systemd stop timeout. After two seconds nothing was listening, both
# verification curls failed with "Couldn't connect", and the script reported a
# WARNING about a grant that had in fact installed perfectly.
#
# A check that cries wolf about its own impatience is worse than no check: it
# teaches the reader to disregard the one time it is right.
say "waiting for mudhorn-web to accept connections"
for _ in $(seq 1 60); do
  curl -sS --max-time 2 -o /dev/null http://127.0.0.1:8787/healthz 2>/dev/null && break
  sleep 2
done

# Proof rather than hope, and asked of the RUNNING SERVICE rather than from
# this shell — a `sudo -u mudhorn sudo -u hermes` here runs in the console's
# mount namespace where everything is visible, and passed cleanly once while
# the real thing was failing on `cd /home/hermes: Permission denied`. See
# enable-chat.sh for the full account of why that check was worse than none.
say "asking the running service to reach the dreamer (may take a minute)"

ENV_FILE="$APP_DIR/.env"
password="$(grep -oP '^DASHBOARD_PASSWORD=\K.*' "$ENV_FILE" 2>/dev/null || true)"
chat_token="$(grep -oP '^DASHBOARD_CHAT_TOKEN=\K.*' "$ENV_FILE" 2>/dev/null || true)"
jar="$(mktemp)"; body="$(mktemp)"
trap 'rm -f "$jar" "$body"' EXIT

if [[ -n "$password" ]]; then
  curl -sS --max-time 20 -c "$jar" -o /dev/null \
    -X POST -d "password=$password" http://127.0.0.1:8787/login || true
fi

code="$(curl -sS --max-time 200 -b "$jar" -o "$body" -w '%{http_code}' \
  -X POST -H 'content-type: application/json' \
  -d "{\"token\":\"$chat_token\",\"soul\":\"grogu\",\"message\":\"Reply with the single word: READY\"}" \
  http://127.0.0.1:8787/chat)" || code=000

if [[ "$code" != "200" ]]; then
  say "WARNING: POST /chat returned $code rather than 200."
  say "         Check: journalctl -u mudhorn-web -n 30"
elif grep -q '"ok": *true' "$body" || grep -q '"ok":true' "$body"; then
  say "The dreamer answered."
else
  say "WARNING: the service reached Hermes and got an error back:"
  sed -n 's/.*"error": *"\([^"]*\)".*/           \1/p' "$body" | head -3
fi

printf '\nDone. Open /dreaming; the banner should no longer say the dreamer\n'
printf 'shares the account agent instance.\n'
printf 'Off again: sudo %s --off\n' "$0"
