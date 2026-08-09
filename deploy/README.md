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
| `mudhorn-backup.timer` | Hourly snapshot of the journal | nothing |
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

**If you rebooted before filling in `.env`, expect a wall of old tracebacks.**
The units are enabled at provisioning time, so a reboot starts them whether or
not credentials exist yet. With a blank `.env` the bot exits with
`Could not resolve authentication method`, systemd retries five times, hits its
rate limit, and stops. That is correct behaviour, and `systemctl start` clears
it once the keys are in — but `journalctl -n 30` will show you the dead run
rather than the live one, because the failed run is longer. Scope the log to the
run you care about:

```sh
journalctl -u mudhorn-bot --since "$(systemctl show -p ActiveEnterTimestamp --value mudhorn-bot)" --no-pager
```

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

**Turn off key expiry on the droplet, in the Tailscale admin console.**
Machines → the droplet → the ⋯ menu → *Disable key expiry*.

Tailscale node keys expire after about six months by default. On a laptop that
is a mild annoyance; on this box it means the dashboard stops being reachable
and **nothing says so** — the bot keeps trading, the service stays green, and
the only symptom is a private address that no longer answers. Worse, the
documented way to fix it is to SSH in and re-authenticate, which is itself over
the network you have just lost.

So: disable expiry on the server node, and keep the droplet's **public IPv4**
written down somewhere outside this repository. It is the fallback that works
when Tailscale does not, and the DigitalOcean control panel is the only place
it lives otherwise.

The alternative for desk use only is an SSH tunnel, which needs no extra account
but has to be re-run each time and is awkward from a phone:

```sh
# Log in as root or as your own sudo user, NOT as mudhorn: bootstrap.sh creates
# that account with /usr/sbin/nologin, so it deliberately cannot be SSHed into.
# That is the point of it — it holds the broker credentials and is not something
# anyone signs in as.
ssh -L 8787:127.0.0.1:8787 root@<vps-ip>    # then browse to 127.0.0.1:8787
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

[`alpacahq/cli`](https://github.com/alpacahq/cli) submits orders from a shell,
so putting it on this box leaves a bypass of all four of the operator's rules
one command away from anything with a prompt. Its read-only value is already
covered by the dashboard, `get_risk_status` and `smoketest`.

Hermes' `terminal` toolset **is** dropped now, in `deploy/hermes-config.yaml`,
which weakens the original argument without retiring the rule. A dropped
toolset is a line in a YAML file, and it fails silently: a duplicate `agent:`
key, a bad merge or an upgrade that renames the setting all end with the agent
holding a shell again and nothing on screen to say so. An order binary that is
not installed cannot be reached by a mistake of that kind.

Same reasoning applies to the OAuth apps in Alpaca's **Connect** tab, which are
order paths that do not pass through `src/bot/risk.py`.

---

## Hermes gateway

Optional, and the only reason this box has to be always on. Install it under its
own `hermes` account per `docs/HERMES_SETUP.md`, **never under `mudhorn`**.
`mudhorn` owns `.env` and therefore the Alpaca credentials; the split is the
entire point, and it leaves the agent reaching the broker only by sudo'ing to
one wrapper script under a single rule in `/etc/sudoers.d/hermes-mcp`. Running
it as `mudhorn` would hand it the credentials directly and make every other
control here decorative.

That doc also covers why the **native gateway path** is mandatory rather than
the Desktop or ACP relay modes: those let the client auto-approve tool
permissions, which is acceptable for a general assistant and not for something
holding broker credentials.

Apply this repo's config with `deploy/merge-hermes-config.py`, not by appending
the YAML. Hermes writes `agent:`, `skills:`, `approvals:` and `mcp_servers:`
itself on first run, so an append duplicates all four and PyYAML silently keeps
only the last of each.

If you skip Hermes, nothing here needs to stay running between sessions and the
VPS becomes optional.

## Backups

`data/journal.db` is the only irreplaceable file on the box, and until recently
nothing copied it. `deploy/backup-journal.sh` now runs hourly under
`mudhorn-backup.timer`.

```sh
systemctl list-timers mudhorn-backup            # when it last ran and next runs
journalctl -u mudhorn-backup -n 20              # what it did
ls -la /opt/mudhorn/backups/hourly              # the snapshots themselves
```

Snapshots land in `/opt/mudhorn/backups/hourly` and are kept for four days. The
first of each day is hard-linked into `backups/daily` and kept for ninety, so a
day's copy costs nothing until the hourly one is pruned.

**It uses `sqlite3 .backup`, never `cp`.** The bot may be part-way through a
write, and SQLite keeps a WAL and an shm alongside the database. A `cp` taken
between two of those writes produces a file that opens fine and reports
corruption later, at whatever moment something reads the wrong page. The online
backup API takes a read lock and copies page by page instead.

Two details are load-bearing and worth not removing:

- **A busy timeout is set on the connection.** Without it `.backup` returns
  "database is locked" the moment the bot is mid-write, and the snapshots that
  fail are exactly the ones taken during the activity worth keeping. It fails
  closed rather than corrupt, so the symptom is a missing backup nobody notices.
- **Every snapshot is opened and integrity-checked before it is kept**, under a
  temporary name, and discarded if it does not come back `ok`. A backup nobody
  has opened is a hope.

Before the bot has ever run there is no journal. The unit carries
`ConditionPathExists`, so systemd records those runs as skipped rather than
failed. Once the file exists, a missing database is a real error and the script
exits non-zero.

### Restoring

Stop the services first. Restoring underneath a running process leaves it
holding a handle to the database it thinks it has, and the stand-down state it
reloads afterwards is anyone's guess.

```sh
sudo systemctl stop mudhorn-bot mudhorn-web
gunzip -c /opt/mudhorn/backups/daily/journal-2026-08-09.db.gz \
  | sudo -u mudhorn tee /opt/mudhorn/data/journal.db >/dev/null
sudo -u mudhorn sqlite3 /opt/mudhorn/data/journal.db 'PRAGMA integrity_check;'
sudo systemctl start mudhorn-bot mudhorn-web
```

The next cycle reconciles the restored journal against the broker, so a position
opened after the snapshot is picked up as untracked and reported rather than
silently ignored. Its planned stop is genuinely unknown at that point, so open
risk is flagged as understated rather than guessed at.

**A backup on the same droplet survives a bad restore, not a dead droplet.**
Pulling `backups/daily` off the box periodically is the other half of this, and
is not automated here because it needs a destination and a credential that
should not live on the trading box.

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
