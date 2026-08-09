# Deploying to a VPS

Turnkey setup for a box that runs the bot without anyone logged in. Aimed at a
fresh **Ubuntu 24.04** server.

This is the handover configuration. It is not the only way to run the bot: the
whole thing works on a laptop from `SETUP.md`, and moving from here back to a
local machine is deleting two systemd units. Nothing below is a one-way door.

---

## What ends up running

| Unit | What it does | Binds |
|---|---|---|
| `mudhorn-bot` | The decision loop. Proposes, vets, reconciles the journal | nothing |
| `mudhorn-web` | The read-only dashboard | `127.0.0.1:8787` |
| Hermes gateway | Chat, if you want it. Installed separately, see below | nothing |

CPU is idle almost all the time at a 15-minute cadence. **Buy RAM, not cores** —
the box runs five processes once Hermes and its MCP children are up. 2 GB is
comfortable, 1 GB wants a swap file. `docs/COSTS.md` has the arithmetic.

---

## 1. Provision

### Fastest path: cloud-init

If the repository is public, `deploy/cloud-init.yaml` does steps 1 and 3 of this
runbook unattended. Edit the `REPO_URL` line, then paste the file into
DigitalOcean's **User data** box when creating the droplet (Advanced Options →
Add Initialization scripts).

The box comes up with the code cloned, the service account created, the venv
built, both systemd units installed and enabled, `ufw` allowing SSH only, and
Tailscale installed but not joined. You SSH in once to fill `.env`.

**No credential goes in that file, deliberately.** DigitalOcean displays user
data in the control panel, and the droplet's own metadata service serves it back
to anything running on the machine — including the bot's service account. Broker
keys and Tailscale auth keys are added over SSH afterwards, which takes a minute.

Skip to §2 if you use it. The manual equivalent follows.

### Manual

**The repository is private, so the box needs its own read credential.** Use a
**deploy key** rather than a personal access token: a deploy key is scoped to
this one repository and read-only, where a PAT carries the whole account with it
and would sit on a machine that also holds broker credentials.

On the box:

```sh
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/mudhorn_deploy
sudo cat /root/.ssh/mudhorn_deploy.pub
```

Paste that public key into **GitHub → the repo → Settings → Deploy keys → Add
deploy key**, and leave "Allow write access" unticked. Then:

```sh
sudo git -c core.sshCommand="ssh -i /root/.ssh/mudhorn_deploy" \
  clone git@github.com:<owner>/<repo>.git /opt/mudhorn
sudo git -C /opt/mudhorn config core.sshCommand "ssh -i /root/.ssh/mudhorn_deploy"
sudo /opt/mudhorn/deploy/bootstrap.sh
```

The second line makes later `git pull`s use the same key without repeating the
flag.

> If you use a personal access token instead, **do not leave it in the remote
> URL** — `git clone https://<token>@github.com/...` writes it into
> `.git/config` in plaintext, where it outlives whatever you were doing. Scrub
> it immediately with
> `sudo git -C /opt/mudhorn remote set-url origin https://github.com/<owner>/<repo>.git`
> and re-supply the token on each pull.

The script installs Python and build tools, creates a `mudhorn` system account
with no login shell, builds the virtualenv, copies `.env.example` to `.env` at
mode 600, and installs both systemd units **enabled but not started**.

Stopping short of starting is deliberate. At that point `.env` is empty, and a
service that boot-loops on a missing credential is harder to read than one that
was never asked to run.

It is idempotent. Re-run it after a `git pull` to pick up new dependencies.

### Who owns what, and why

`src/`, `config/` and `deploy/` stay **root-owned**; only `data/` and `audit/`
belong to the service account. The bot can therefore write its journal and its
audit log but cannot rewrite its own code or edit `config/rules.yaml`.

That is the same principle as the risk gate itself. The limits are enforced by
code the running process is not able to modify, so changing a limit means a
commit, on purpose, by a person.

## 2. Credentials

```sh
sudo -e /opt/mudhorn/.env
```

Alpaca paper keys and the Anthropic key. **Leave `ALPACA_PAPER_TRADE=true`** —
the code refuses to start twice over without it.

## 3. Prove it before trusting it

```sh
cd /opt/mudhorn && sudo -u mudhorn .venv/bin/electrum-bot smoketest
```

Connects, prints equity, cash, position count and tick count, asks Claude one
question, and **places nothing**. If that prints an equity figure the whole chain
is wired.

**The `cd` is load-bearing.** `src/bot/config.py` sets `env_file=".env"`, a
relative path, so credentials are found relative to the working directory. Run
this from `/root` instead and it tries to read `/root/.env`, which the service
account cannot even stat — a `PermissionError` traceback that looks like the
`.env` you just wrote is broken when it is fine. The systemd units are unaffected:
both set `WorkingDirectory=/opt/mudhorn`.

A fresh Alpaca paper account starts at **$100,000**, which matches
`min_equity_floor_usd: 90000` in `config/rules.yaml`. Reset the paper account to
a different balance and that floor needs changing to match, or the gate will
refuse every trade.

## 4. Start

```sh
sudo systemctl start mudhorn-bot mudhorn-web
systemctl status mudhorn-bot
journalctl -u mudhorn-bot -f
```

Both restart on failure and come back after a reboot.

## 5. Reach the dashboard at all

On a laptop the dashboard was on the same machine as the browser, so
`127.0.0.1:8787` just worked and Tailscale was a convenience for viewing it on a
phone. **On a VPS that is no longer true.** The dashboard binds to loopback on a
box you are not sitting at, so without a private link there is no route to it
from anywhere, laptop included.

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install Tailscale on the phone and laptop too, then browse to the box's private
address on port 8787. Free for personal use, and it signs in with the project
Google account rather than needing another password.

The alternative for desk use only is an SSH tunnel, which needs no extra account
but has to be re-run each time and is awkward from a phone:

```sh
ssh -L 8787:127.0.0.1:8787 mudhorn@<vps-ip>    # then browse to 127.0.0.1:8787
```

What you should **not** do is reach for a public hostname to avoid installing
something. See below.

**Do not put the dashboard on a public URL.** It renders account equity, open
positions and realised P&L, and it has no login *because* it binds to
`127.0.0.1`. Those two facts are load-bearing together. Publishing it means
building real authentication first, and that is a project rather than a step.

The identity page under `brand/` is public and that is fine, because it reads no
journal, no broker and no credential. It is not a precedent for this.

---

## Live execution is off, and turning it on is a decision

The unit runs `electrum-bot loop` with **no `--execute` flag**. The bot proposes
orders, runs them through the risk gate, records the outcome, and places nothing.

That is the correct state for a handover. Watch the proposals for a few weeks
first and see whether you agree with them. Turning execution on means editing
`ExecStart` in `/etc/systemd/system/mudhorn-bot.service` to append `--execute`,
then `systemctl daemon-reload && systemctl restart mudhorn-bot`.

It stays paper either way. `ALPACA_PAPER_TRADE=false` is refused by the code.

## Do not install the Alpaca CLI here

[`alpacahq/cli`](https://github.com/alpacahq/cli) submits orders from a shell.
Hermes cannot drop its `terminal` toolset, so putting it on this box leaves a
bypass of all four of the operator's rules one command from the agent. Its
read-only value is already covered by the dashboard, `get_risk_status` and
`smoketest`.

Same reasoning applies to the OAuth apps in Alpaca's **Connect** tab, which are
order paths that do not pass through `src/bot/risk.py`.

---

## Hermes gateway

Optional, and the only reason this box has to be always on. Install it under the
`mudhorn` account per `docs/BUZZ_SETUP.md`, which covers why the **native
gateway path** is mandatory rather than the Desktop or ACP relay modes: those let
the client auto-approve tool permissions, which is acceptable for a general
assistant and not for something holding broker credentials.

If you skip Hermes, nothing here needs to stay running between sessions and the
VPS becomes optional.

## Updating

```sh
cd /opt/mudhorn
sudo git pull
sudo /opt/mudhorn/deploy/bootstrap.sh          # picks up dependency changes
sudo systemctl restart mudhorn-bot mudhorn-web
```

## Moving off the VPS

`data/journal.db` is the only irreplaceable file. It holds every trade, the
equity curve and the persistent stand-down state. Copy it to the new machine and
the history and any active stand-down travel with it; leave it behind and the
new box starts with a clean journal and a cleared breaker, which is data loss
rather than a fresh start.

```sh
sudo -u mudhorn sqlite3 /opt/mudhorn/data/journal.db ".backup '/tmp/journal.db'"
```

Taking a copy that way rather than `cp` avoids catching the database mid-write.
