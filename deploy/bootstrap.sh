#!/usr/bin/env bash
#
# Provision a fresh Ubuntu box to run the bot as a service.
#
# Idempotent: safe to re-run after a `git pull` to pick up new dependencies.
#
#   sudo /opt/mudhorn/deploy/bootstrap.sh
#
# It deliberately does NOT start anything. The services are enabled but left
# stopped, because at this point there are no credentials in .env and a service
# that boot-loops on a missing key is harder to diagnose than one that has not
# been asked to run yet. `deploy/README.md` picks up from there.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
APP_USER="${APP_USER:-mudhorn}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "No checkout at $APP_DIR." >&2
  echo "Clone the repo there first, then re-run:" >&2
  echo "  sudo git clone <repo-url> $APP_DIR" >&2
  exit 1
fi

# ------------------------------------------------------- WHICH code is this
#
# **"Provisioned." used to be a claim about a tree nobody had identified**, and
# that cost a deploy. Observed 13 Aug 2026: two wrappers had been hand-edited on
# the box, so `git pull` aborted with "your local changes would be overwritten",
# this script then ran happily against the OLD checkout and printed
# "Provisioned.", and the verification step afterwards reproduced the exact bug
# the pull was meant to fix. Every line of output was true and the deploy had
# not happened.
#
# The operator ran four commands as one paste, so the abort scrolled past above
# a wall of successful-looking output. That is the shape to defend against: a
# step that silently did not happen, under a summary that reads as though it
# did. Same rule as `calendar_degraded` and the tailnet status — report the
# weaker fact rather than let silence read as the stronger one.
#
# So this states the commit it is provisioning FROM, and says loudly when the
# tree is dirty. It does not refuse: a deliberate local edit is a legitimate
# thing to be doing on a box, and refusing to provision would be the
# config-load validator mistake in a new place. It has to be impossible to miss,
# not impossible to do.
if [[ -d "$APP_DIR/.git" ]] && command -v git >/dev/null 2>&1; then
  echo "==> Checkout"
  # `safe.directory`: this runs as root over a tree git may consider another
  # user's, and a refusal here must cost the READING rather than the deploy.
  GIT="git -c safe.directory=$APP_DIR -C $APP_DIR"
  DESCRIBED="$($GIT log -1 --format='%h %s' 2>/dev/null || echo 'unknown')"
  BRANCH="$($GIT rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
  echo "    $BRANCH at $DESCRIBED"

  DIRTY="$($GIT status --porcelain 2>/dev/null || true)"
  if [[ -n "$DIRTY" ]]; then
    echo ""
    echo "    *** UNCOMMITTED CHANGES IN $APP_DIR ***"
    echo "$DIRTY" | sed 's/^/      /'
    echo ""
    echo "    A dirty tree makes 'git pull' ABORT. If you just ran one and did"
    echo "    not read every line, this box may still be on the old code while"
    echo "    everything below reports success. Check what the pull actually"
    echo "    said before trusting the commit named above."
    echo ""
  fi
fi

echo "==> Packages"
apt-get update -qq
# python3-venv is separate from python3 on Debian and Ubuntu, and its absence is
# the single most common reason a first deploy fails.
# sqlite3 is the CLI, not the Python module. deploy/backup-journal.sh needs it
# for the online backup API, which is the only safe way to copy a database the
# bot may be writing to.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential git curl ca-certificates sqlite3

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "    python3 is $PY_VERSION"
python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(
        "pyproject.toml requires Python 3.11 or newer. This box is older.\n"
        "On Ubuntu 22.04 or earlier, add the deadsnakes PPA or use Ubuntu 24.04."
    )
EOF

echo "==> Service account"
# A system account with no login shell. The bot holds broker credentials, so it
# should not be something anyone signs in as.
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  echo "    created $APP_USER"
else
  echo "    $APP_USER exists"
fi

echo "==> Directories"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 \
  "$APP_DIR/data" "$APP_DIR/audit" \
  "$APP_DIR/backups" "$APP_DIR/backups/hourly" "$APP_DIR/backups/daily"

echo "==> Virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet -e "$APP_DIR"

echo "==> Credentials file"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    created .env from the example. It is EMPTY and must be filled in."
else
  echo "    .env exists, leaving it alone"
fi
# Readable only by the service account, whatever the umask happened to be.
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# The checkout itself stays root-owned so the service account cannot rewrite its
# own code. Only the two paths that must be written at runtime are handed over.
#
# `config/` stays here EVEN THOUGH the settings agent can now change a limit.
# It changes one by asking root to, through deploy/apply-settings.sh, which
# validates the whole file before replacing it — the file is still only ever
# written by root, and handing it to the service account to save a wrapper would
# trade that for nothing. A bot that can rewrite its own limits with its own
# hands is what this line exists to prevent.
#
# `souls/` is here for the same reason and was MISSING, which is worth stating
# rather than quietly adding. Each soul's `## What to avoid` section is a
# safety rail — never propose a trade, never state a figure you did not read,
# never learn from the track record — and `souls.py` reads these files from
# disk at call time, so whatever is on the box at that moment is what reaches
# the model. Left writable by the service account, the rails are editable by
# the thing they restrain. Nothing is known to have exploited that; it was
# simply never on the list.
chown -R root:root "$APP_DIR/src" "$APP_DIR/config" "$APP_DIR/deploy" \
  "$APP_DIR/souls"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/audit" "$APP_DIR/backups"

echo "==> systemd units"
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-bot.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-web.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-backup.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-backup.timer" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-tailnet.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-tailnet.timer" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-dream.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-dream.timer" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-confer.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-confer.timer" /etc/systemd/system/
# Installed, never enabled, and it is the one unit here that runs as ROOT.
# It also refuses to start without /etc/mudhorn-console.env, which bootstrap
# deliberately does NOT create: a console whose token was generated by a
# provisioning script is a console whose token is in a provisioning log.
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-console.service" /etc/systemd/system/
# run-mcp.sh, run-chat.sh, run-dream.sh and apply-settings.sh are named in
# sudoers rules, so they must be executable and — from the chown above —
# root-owned and not writable by the account the rule grants FROM. A wrapper
# writable by that account turns its sudoers rule into arbitrary code execution
# as the target user.
#
# run-dream.sh is made executable here whether or not the second Hermes instance
# exists. It is inert without one: nothing invokes it unless a sudoers rule names
# it, and the dashboard falls back to the account agent and says so on the page.
#
# apply-settings.sh is the same, and its inertness is load-bearing rather than
# convenient. It is the ONE wrapper here whose sudo runs upward — `mudhorn` to
# root — so it is installed and not granted: without a rule in
# /etc/sudoers.d/mudhorn-forge nothing can invoke it, the settings agent reports
# that it cannot write the file, and the change stays a recorded request a
# person applies. `deploy/enable-forge.sh` is the deliberate act that grants it.
chmod 755 "$APP_DIR/deploy/backup-journal.sh" "$APP_DIR/deploy/check-tailscale.sh" \
          "$APP_DIR/deploy/run-mcp.sh" "$APP_DIR/deploy/run-chat.sh" \
          "$APP_DIR/deploy/run-dream.sh" "$APP_DIR/deploy/apply-settings.sh" \
          "$APP_DIR/deploy/enable-forge.sh" "$APP_DIR/deploy/update.sh" \
          "$APP_DIR/deploy/enable-dream.sh" "$APP_DIR/deploy/run-research.sh" \
          "$APP_DIR/deploy/enable-research.sh" "$APP_DIR/deploy/enable-console.sh"
systemctl daemon-reload
systemctl enable --quiet mudhorn-bot.service mudhorn-web.service
echo "    enabled at boot, not started"

# The timer IS started here, unlike the two services. It costs nothing while
# there is no journal to snapshot, the unit skips cleanly until one exists, and
# a backup that waits for somebody to remember to switch it on is the backup
# that turns out not to have been running.
systemctl enable --now --quiet mudhorn-backup.timer
echo "    mudhorn-backup.timer started (hourly)"

# The dream timer is installed but NOT started, and that asymmetry is the point.
# The backup timer costs nothing and the failure it prevents is unrecoverable,
# so it starts itself. This one spends money on an API call every time it fires
# and needs a model credential to do anything at all — without one it exits 1
# and would post a failed unit every morning, which teaches an operator to
# ignore systemctl --failed. Same reasoning as --execute and the chat token:
# anything that spends or acts is switched on by a person who decided to.
# **Report the timer's REAL state, never a constant.** These two lines used to
# be bare `echo`s saying "installed, NOT started", printed on every provision
# whether the timer was running or not. Observed 17 Aug 2026: the dream timer
# had been firing daily for weeks — `systemctl list-timers` showed it had run
# that very morning — while every deploy told the operator it was off, and told
# them to run an `enable` that was already done.
#
# That is this repository's founding failure in the provisioning script: a true-
# sounding line stating something nobody checked. It cost a real diagnosis —
# a session read the banner instead of the system, concluded the dreamer had
# never run on a schedule, and looked for the backlog's cause in the wrong
# place entirely.
#
# `is-enabled` and `is-active` are different questions and both are asked:
# enabled-but-inactive is a timer that will come back after a reboot and is not
# counting down now, which neither "started" nor "NOT started" describes.
timer_state() {
  local unit="$1" enabled active
  enabled="$(systemctl is-enabled --quiet "$unit" && echo yes || echo no)"
  active="$(systemctl is-active --quiet "$unit" && echo yes || echo no)"
  if [[ "$enabled" == yes && "$active" == yes ]]; then
    echo "running"
  elif [[ "$active" == yes ]]; then
    echo "running, but not enabled at boot"
  elif [[ "$enabled" == yes ]]; then
    echo "enabled at boot, not currently active"
  else
    echo "installed, NOT started"
  fi
}

report_timer() {
  local unit="$1" state
  state="$(timer_state "$unit")"
  echo "    $unit $state"
  if [[ "$state" == "installed, NOT started" ]]; then
    echo "      enable with: systemctl enable --now $unit"
    return 1
  fi
  # The next firing, which is the figure that actually answers "is this
  # working". A timer can be enabled and active and still never fire if its
  # calendar expression does not parse.
  systemctl list-timers --all --no-legend "$unit" 2>/dev/null \
    | awk '{print "      next: " $1 " " $2 " " $3}' | head -1
  return 0
}

if ! report_timer mudhorn-dream.timer; then
  echo "      costs roughly a few pounds a year."
fi
# Said here because this is the line somebody reads while wondering which key
# this timer needs, and the answer changed. It used to be ANTHROPIC_API_KEY
# unconditionally and the note here said the DigitalOcean switch did not reach
# it. That is no longer true: `electrum-bot dream` follows whichever provider
# .env configures, and it authenticates with that provider's OWN key. Naming
# the wrong variable sends an operator to the wrong console.
echo "      needs the configured provider's key in .env: DO_INFERENCE_KEY when"
echo "      that is set, ANTHROPIC_API_KEY otherwise. The command says which."

# The conference timer, on the same footing and for the same reasons: it spends
# money on model calls and needs the same key. It fires an hour after the dream
# so that a conference always has that morning's dream to talk about, and it is
# worth nothing without the dream timer — an empty vault means every run is a
# no-op — so enabling it alone is the odd configuration rather than the useful
# one. An accept grants a SYMBOL PERMISSION with an expiry; it places nothing,
# and RiskGate still runs on anything traded under one.
if ! report_timer mudhorn-confer.timer; then
  echo "      enable the dream timer too, or it has nothing to confer about."
fi

# Started for the same reason. Before Tailscale is installed this reports "not
# logged in", which is correct rather than noisy: on a box whose dashboard is
# only reachable over Tailscale, not having it is a real finding on day one.
systemctl enable --now --quiet mudhorn-tailnet.timer
echo "    mudhorn-tailnet.timer started (every 6 hours)"

cat <<EOF

Provisioned. Nothing is running yet, on purpose.

Next, in order:

  1. Put the Alpaca paper keys and the Anthropic key into $APP_DIR/.env
         sudo -e $APP_DIR/.env
     Leave ALPACA_PAPER_TRADE=true. The code refuses to start without it.

  2. Prove the credentials work before running anything as a service:
         cd $APP_DIR && sudo -u $APP_USER .venv/bin/electrum-bot smoketest
     It connects, prints equity, and places nothing.
     The 'cd' matters: .env is found relative to the working directory, so
     running this from elsewhere fails with a confusing PermissionError.

  3. Start:
         sudo systemctl start mudhorn-bot mudhorn-web
         systemctl status mudhorn-bot

  4. Reach the dashboard from a phone with Tailscale:
         curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
     It binds to 127.0.0.1 and sits behind ONE shared password, from
     DASHBOARD_PASSWORD in $APP_DIR/.env. With that set it may be exposed
     publicly; with it UNSET there is no gate at all, and this process cannot
     tell the two apart -- a Funnel and a local curl both arrive on loopback.
     electrum-bot-web says which mode it is in at startup, because that is the
     only moment anyone can be told. Read that line.

  5. Optional, and only once Hermes is installed: point the three souls at
     DigitalOcean Gradient inference instead of Anthropic.
     The key does NOT go in $APP_DIR/.env. \`hermes\` cannot read that file,
     which is the user split doing its job, so the souls carry their own
     credential in their own home — one file per Hermes instance, so the
     dreamer can run a different model from the account agent:
         sudo -u hermes touch /home/hermes/inference.env
         sudo -u hermes chmod 600 /home/hermes/inference.env
         # then DO_INFERENCE_KEY / DO_INFERENCE_MODEL, one per line
     Prove the model slug BEFORE the first message. deploy/README.md,
     "Pointing the souls at DigitalOcean", has the check that reads the served
     model back.

  6. The PYTHON model calls -- the loop, the dreamer and the conference -- move
     with a SEPARATE key, and this one DOES go in $APP_DIR/.env:
         DO_INFERENCE_KEY=<a model access key, never a dop_v1_ token>
         DECISION_MODEL_ID=<a model that holds the schema; see docs/DROPLET_AI.md>
         DREAM_MODEL_ID=<likewise>
     Naming the models is not optional here. That endpoint calls Anthropic
     models by different ids and 403s them on this account's tier, so leaving
     the ids unset refuses at startup rather than failing on every cycle.
     Unsetting DO_INFERENCE_KEY is the whole rollback: empty means Anthropic.

The loop runs WITHOUT --execute. It proposes and vets orders and places none.
Read deploy/README.md before changing that.
EOF
