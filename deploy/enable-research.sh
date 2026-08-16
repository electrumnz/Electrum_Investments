#!/usr/bin/env bash
#
# Give the dreamer a researcher: a THIRD Hermes instance that can reach the
# web, and nothing else.
#
#     sudo /opt/mudhorn/deploy/enable-research.sh           # on
#     sudo /opt/mudhorn/deploy/enable-research.sh --off     # back off
#     sudo /opt/mudhorn/deploy/enable-research.sh --status  # what is set now
#
# ## What this is for
#
# The dreamer reasons about cicada broods and smelter restarts with no way to
# look anything up, so every hop it writes is reference knowledge it already
# had or an invention — and `Hop.checked` exists because some of them were
# inventions. Measured on the live shelf: one hop sourced out of six, and the
# one unsourced hop that had a real story behind it could not be sourced
# either, because the parser had thrown the URL away.
#
# The researcher goes and looks. It returns QUOTES and URLs and never
# conclusions, because a summary launders provenance: the distilled sentence
# has no author and no date, and after the distillation a reader cannot tell
# which half was published and which half the model supplied.
#
# ## Why a third instance
#
# Hermes loads one soul and one MCP registry per `HERMES_HOME`, and the
# argument runs in both directions:
#
#   - The account agent's instance registers this repo's MCP server, which
#     exposes `place_order`. A page is untrusted text written by anyone;
#     putting a web reader in the process that can place orders is the one
#     arrangement to avoid.
#   - The dreamer's instance has no MCP server and is the one that would most
#     like the web — but giving it a fetch tool puts a fetched page and a
#     speculative causal chain in one context with nothing between them.
#
# ## The sandbox has to be relaxed, and that is the honest cost
#
# `mudhorn-dream.service` sets NoNewPrivileges, RestrictSUIDSGID and
# ProtectHome. All three block `sudo` — the second by IMPLYING the first, which
# is the one people miss — so the researcher cannot run from the dream timer
# with the unit as shipped. This installs a drop-in that relaxes exactly those
# three and nothing else. See `deploy/systemd/mudhorn-dream-research.conf` for
# what that buys and what it gives up.
#
# It fails SAFE without it rather than silently: `Researcher.enabled` asks
# `sudo -n -l`, gets a refusal, and every answer carries an error saying no
# researcher is permitted. A hop then stays unchecked, which is the truth.

set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
SUDOERS=/etc/sudoers.d/mudhorn-research
WRAPPER="$APP_DIR/deploy/run-research.sh"
RULE="mudhorn ALL=(hermes) NOPASSWD: $WRAPPER"
RESEARCH_HOME=/home/hermes/researcher
SOUL_SRC="$APP_DIR/souls/kuiil.md"
CONFIG_SRC="$APP_DIR/deploy/hermes-research-config.yaml"
DROPIN_DIR=/etc/systemd/system/mudhorn-dream.service.d
DROPIN="$DROPIN_DIR/research.conf"
DROPIN_SRC="$APP_DIR/deploy/systemd/mudhorn-dream-research.conf"

die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run this with sudo"

mode="${1:-on}"

# ------------------------------------------------------------------- status

if [[ "$mode" == "--status" ]]; then
  printf '\nResearcher instance\n'
  [[ -f "$SUDOERS" ]] && say "sudoers rule: installed" || say "sudoers rule: ABSENT"
  [[ -f "$DROPIN" ]] && say "dream sandbox drop-in: installed" \
    || say "dream sandbox drop-in: ABSENT — sudo is blocked from the dream timer"
  [[ -d "$RESEARCH_HOME" ]] && say "home: $RESEARCH_HOME" || say "home: ABSENT"
  [[ -f "$RESEARCH_HOME/SOUL.md" ]] && say "soul: installed" || say "soul: ABSENT"
  [[ -f "$RESEARCH_HOME/inference.env" ]] \
    && say "credential: present" || say "credential: ABSENT"
  if [[ -f "$RESEARCH_HOME/.hermes/config.yaml" ]]; then
    if sed 's/#.*//' "$RESEARCH_HOME/.hermes/config.yaml" \
         | grep -qE 'run-mcp\.sh|electrum-bot|mcp_servers'; then
      say "MCP: REGISTERED — this instance can reach the broker, which defeats the point"
    else
      say "MCP: not registered (correct)"
    fi
  else
    say "config: ABSENT"
  fi
  exit 0
fi

# ---------------------------------------------------------------------- off

if [[ "$mode" == "--off" ]]; then
  printf '\nTurning the researcher off\n'
  rm -f "$SUDOERS" && say "removed $SUDOERS"
  # The drop-in goes too. It exists ONLY to let this one sudo through, so
  # leaving a relaxed sandbox behind after withdrawing the grant would be the
  # cost with none of the benefit — the worst of both.
  if [[ -f "$DROPIN" ]]; then
    rm -f "$DROPIN"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    say "removed the sandbox drop-in and re-tightened mudhorn-dream.service"
  fi
  # The home is left alone deliberately, as in enable-dream.sh: it holds the
  # researcher's own memory, and deleting an agent's history to withdraw a
  # permission is a far bigger act than the one being asked for. Without the
  # sudoers rule it is unreachable.
  printf '\nDone. Hops stay unchecked and every answer says why, which is the truth.\n'
  printf 'The home at %s is left in place; it is unreachable without the rule.\n' "$RESEARCH_HOME"
  exit 0
fi

[[ "$mode" == "on" ]] || die "unknown option: $mode (use --off or --status)"

# ----------------------------------------------------------------------- on

printf '\nTurning the researcher on\n'

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

# 1. The home. `run-research.sh` does `cd "$HERMES_HOME"` and dies without it.
if [[ -d "$RESEARCH_HOME" ]]; then
  say "home already exists at $RESEARCH_HOME"
else
  install -d -m 700 -o hermes -g hermes "$RESEARCH_HOME"
  say "created $RESEARCH_HOME"
fi

# 2. The soul. Kuiil ships and no chat request may select it — `KNOWN_SOULS` is
#    narrower than `ALL_SOULS` precisely because the Chat page's instance is the
#    one holding `place_order`.
if [[ -f "$SOUL_SRC" ]]; then
  install -m 600 -o hermes -g hermes "$SOUL_SRC" "$RESEARCH_HOME/SOUL.md"
  say "installed kuiil.md as $RESEARCH_HOME/SOUL.md"
else
  say "WARNING: $SOUL_SRC not found; the researcher will run voiceless"
fi

# 3. The credential, copied from the account agent's home.
#
# **A grant to an unfinished instance is worth LESS than no grant**, and that
# was measured the first time `enable-dream.sh` ran: home, soul and sudoers all
# installed cleanly, the dashboard duly routed to the isolated instance, and
# that instance had no credential — so it answered "No inference provider
# configured" where the fallback had worked a minute earlier.
#
# The same DigitalOcean key as the other two, deliberately. A second key is a
# second thing to rotate and a second balance to top up, and the isolation this
# buys is about TOOLS, not billing.
SHARED_ENV=/home/hermes/inference.env
RESEARCH_ENV="$RESEARCH_HOME/inference.env"

if [[ -f "$RESEARCH_ENV" ]]; then
  say "inference.env already present, leaving it"
elif [[ -f "$SHARED_ENV" ]]; then
  install -m 600 -o hermes -g hermes "$SHARED_ENV" "$RESEARCH_ENV"
  say "copied inference.env from the account agent"
else
  say "WARNING: $SHARED_ENV not found, so the researcher has no credential."
  say "         Every look-up will come back as an error, which is honest but useless."
fi

# 4. The config. An ALLOWLIST, which is the opposite of the other two homes and
#    is the point: a denylist admits whatever the next Hermes release adds, and
#    here that is a web-reading process quietly gaining a capability nobody
#    chose. Getting an allowlist wrong yields an agent with almost no tools,
#    which is visible and safe.
RESEARCH_CONFIG_DIR="$RESEARCH_HOME/.hermes"
RESEARCH_CONFIG="$RESEARCH_CONFIG_DIR/config.yaml"
model=""
[[ -f "$RESEARCH_ENV" ]] && model="$(grep -oP '^DO_INFERENCE_MODEL=\K.*' "$RESEARCH_ENV" 2>/dev/null || true)"

if [[ -f "$RESEARCH_CONFIG" ]]; then
  say "config.yaml already present, leaving it"
  say "  (check it against $CONFIG_SRC by hand — this script will not overwrite)"
elif [[ -f "$CONFIG_SRC" ]]; then
  install -d -m 700 -o hermes -g hermes "$RESEARCH_CONFIG_DIR"
  tmp_cfg="$(mktemp)"
  cat "$CONFIG_SRC" >"$tmp_cfg"
  if [[ -n "$model" ]]; then
    printf '\nmodel:\n  default: %s\n' "$model" >>"$tmp_cfg"
  else
    say "WARNING: no DO_INFERENCE_MODEL found; the config names no model and"
    say "         the researcher cannot answer until one is set."
  fi
  install -m 600 -o hermes -g hermes "$tmp_cfg" "$RESEARCH_CONFIG"
  rm -f "$tmp_cfg"
  say "wrote $RESEARCH_CONFIG${model:+ with model.default=$model}"
else
  die "$CONFIG_SRC is missing; refusing to write a config with no allowlist"
fi

# 5. The quarantine, checked here as well as by the wrapper at run time. This
#    is the entire argument for a third instance, so it is not taken on trust:
#    a home whose config registers the MCP server is a web-reading agent with
#    `place_order`, which is worse than no third instance at all.
# COMMENTS ARE STRIPPED FIRST, and that is not a detail. The shipped config's
# own header says "there is no `mcp_servers:` key" — so a naive grep matches
# the sentence explaining the absence and kills the install over the very
# comment documenting that it is safe. Found by a test rather than on the box,
# which is the only reason it is not a failed deploy at a console.
if sed 's/#.*//' "$RESEARCH_CONFIG" | grep -qE 'run-mcp\.sh|electrum-bot|mcp_servers'; then
  die "$RESEARCH_CONFIG registers an MCP server. Remove it from THIS home —
       the account agent keeps its own under /home/hermes."
fi
say "quarantine checked: no mcp_servers in $RESEARCH_CONFIG"

# 6. The sudoers rule, validated before it is anywhere sudo will read it. A
#    malformed sudoers file locks the box out of sudo entirely, and the
#    realistic moment somebody runs this is one-handed in a web console.
#
#    Its own file, not `mudhorn-chat` and not `mudhorn-dream`: turning the chat
#    panel off must not withdraw the researcher's grant, and vice versa.
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

# 7. The sandbox drop-in. Without this the grant above is inert: the dream unit
#    blocks sudo three ways and the researcher never runs.
if [[ ! -f "$DROPIN_SRC" ]]; then
  die "$DROPIN_SRC is missing; the grant would be inert without it"
fi
install -d -m 755 -o root -g root "$DROPIN_DIR"
install -m 644 -o root -g root "$DROPIN_SRC" "$DROPIN"
systemctl daemon-reload
say "installed $DROPIN and reloaded systemd"

# Proof rather than hope, and asked THROUGH the same sandbox the dream timer
# uses rather than from this shell. `sudo -u mudhorn sudo -u hermes` typed here
# runs in the console's mount namespace, where /home is visible and everything
# passes — it passed cleanly once while the real thing was failing on
# `cd /home/hermes: Permission denied`. `systemd-run` with the unit's own
# properties is the closest thing to asking the timer itself.
say "asking a sandboxed unit to reach the researcher (may take a minute)"

probe_out="$(mktemp)"
trap 'rm -f "$probe_out"' EXIT

if systemd-run --quiet --pipe --wait --collect \
    --uid=mudhorn \
    --property=NoNewPrivileges=false \
    --property=RestrictSUIDSGID=false \
    --property=ProtectHome=false \
    --property=ProtectSystem=strict \
    --property=PrivateTmp=true \
    /bin/sh -c "echo 'Reply with the single word: READY' | sudo -n -u hermes -- $WRAPPER" \
    >"$probe_out" 2>&1; then
  if grep -qi 'ready' "$probe_out"; then
    say "The researcher answered."
  else
    say "WARNING: it ran but did not answer as expected. First lines:"
    head -3 "$probe_out" | sed 's/^/           /'
  fi
else
  say "WARNING: the probe failed. First lines:"
  head -5 "$probe_out" | sed 's/^/           /'
  say "         Check: journalctl -u mudhorn-dream -n 30"
fi

printf '\nDone. The next `electrum-bot dream` will look up its unchecked hops.\n'
printf 'Citations appear on /dreaming under the chain they were fetched for,\n'
printf 'and the dreamer decides on the following step whether any of them\n'
printf 'actually establishes a hop. Nothing marks a hop checked but the model.\n'
printf 'Off again: sudo %s --off\n' "$0"
