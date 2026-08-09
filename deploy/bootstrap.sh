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

echo "==> Packages"
apt-get update -qq
# python3-venv is separate from python3 on Debian and Ubuntu, and its absence is
# the single most common reason a first deploy fails.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential git curl ca-certificates

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
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/data" "$APP_DIR/audit"

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
chown -R root:root "$APP_DIR/src" "$APP_DIR/config" "$APP_DIR/deploy"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data" "$APP_DIR/audit"

echo "==> systemd units"
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-bot.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/systemd/mudhorn-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet mudhorn-bot.service mudhorn-web.service
echo "    enabled at boot, not started"

cat <<EOF

Provisioned. Nothing is running yet, on purpose.

Next, in order:

  1. Put the Alpaca paper keys and the Anthropic key into $APP_DIR/.env
         sudo -e $APP_DIR/.env
     Leave ALPACA_PAPER_TRADE=true. The code refuses to start without it.

  2. Prove the credentials work before running anything as a service:
         sudo -u $APP_USER $APP_DIR/.venv/bin/electrum-bot smoketest
     It connects, prints equity, and places nothing.

  3. Start:
         sudo systemctl start mudhorn-bot mudhorn-web
         systemctl status mudhorn-bot

  4. Reach the dashboard from a phone with Tailscale. It binds to 127.0.0.1 and
     has no login, so do not put it on a public address:
         curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up

The loop runs WITHOUT --execute. It proposes and vets orders and places none.
Read deploy/README.md before changing that.
EOF
