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
| `mudhorn-dream.timer` | One dream step, 07:00 New Zealand. Installed, NOT started | nothing |
| `mudhorn-confer.timer` | One dream-vault conference, 08:00 New Zealand. Installed, NOT started | nothing |
| Hermes gateway | Chat, if you want it. Installed separately, see below | nothing |

The last two spend money on model calls every time they fire, so `bootstrap.sh`
installs them and leaves them off — the same reasoning as `--execute` and the
chat token. The conference runs an hour after the dream so it always has that
morning's dream to talk about, and it grants at most a **symbol permission with
an expiry**: it reaches no broker, and `RiskGate` still runs on anything traded
under a grant. Both are scheduled by NAMED timezone rather than a converted UTC
hour, because New Zealand observes daylight saving and the drift would be
silent.

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

**Turn off key expiry on the droplet if your tailnet allows it.**
Machines → the droplet → the ⋯ menu → *Disable key expiry*.

Some tailnets cap the maximum node key lifetime (Settings → Device management →
Key expiry) and then that menu item is greyed out. This one caps it at 90 days.
When you cannot disable it, `mudhorn-tailnet.timer` is what stops the expiry
being a surprise — see below.

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

### Watching the key expiry

`mudhorn-tailnet.timer` runs `deploy/check-tailscale.sh` every six hours. It
reads `tailscale status --json`, writes the answer to
`data/tailnet-status.json`, and exits non-zero when a person should act.

```sh
systemctl list-timers mudhorn-tailnet          # when it last ran, when it runs next
systemctl start mudhorn-tailnet.service        # check right now
journalctl -u mudhorn-tailnet -n 5 --no-pager  # what it said
```

**The warning appears as a banner on the dashboard**, which looks backwards —
a warning about losing the dashboard, shown on the dashboard. It is the right
place precisely because the failure is ten days of notice followed by an
outage: during the notice period the page is up and being looked at, and after
the key lapses nothing on this box can reach anyone. The non-zero exit and
`systemctl --failed` are the backstop for nobody having opened it.

It fires at ten days remaining, so on a 90-day cap that is around day 80.
Re-authenticating takes two minutes, so the notice only has to outlast a
holiday, and a banner sitting there for a fortnight stops being read long
before it stops being true.

After you fix it, clear the banner by re-checking — the command is named in the
banner itself so nobody has to remember it:

```sh
tailscale up                                # fix the link
systemctl start mudhorn-tailnet.service     # re-check; the banner goes
```

To remove the reading entirely, for a handover or if you drop the timer:

```sh
sudo -u mudhorn /opt/mudhorn/.venv/bin/python -m bot.tailnet --clear \
  --out /opt/mudhorn/data/tailnet-status.json
```

Three things it deliberately does not do. It does not treat a stale reading as
healthy — a check that stopped running reports the link as *unknown*, because a
file describing a healthy link is not evidence of one. It does not read a
missing expiry date as zero days left; that means expiry is disabled, which is
the good outcome. And it does not report anything when it has never run, so a
box without the timer is not told its link is fine.

## 5b. A public URL, if you want one

The dashboard used to be loopback-only, and the rule was that publishing it
required building real authentication first rather than bolting one on after.
That authentication now exists — `src/bot/web/auth.py` — so a public URL is
supported. It is still a decision, not a default.

**Set the password first.** The app cannot tell whether it is exposed: a
Tailscale Funnel and a reverse proxy both arrive on loopback, so from inside
the process a request from the internet is indistinguishable from a local one.
Nothing will warn you afterwards.

```sh
printf '\nDASHBOARD_PASSWORD=%s\n' 'your-password-here' >> /opt/mudhorn/.env
systemctl restart mudhorn-web
```

Confirm it took, before exposing anything:

```sh
journalctl -u mudhorn-web -n 5 --no-pager   # must say the password is SET
curl -sS -o /dev/null -w '%{http_code}\n' -H 'accept: text/html' http://127.0.0.1:8787/
```

That must print `303` — a redirect to `/login`. A `200` means the password is
not loaded and the dashboard is still open.

Then publish it with a Funnel, which gives real TLS and a real hostname with no
nginx, no certbot and no open port on the droplet:

```sh
tailscale funnel --bg 8787
tailscale funnel status          # prints the public https:// URL
```

`tailscale funnel --https=443 off` takes it down again.

Two things stay true regardless:

- **`POST /chat` keeps its own separate token.** The password lets someone
  *view* the account; `DASHBOARD_CHAT_TOKEN` lets them *drive an agent* that
  reaches the broker. Do not enable the second just because the first is set.
- **One shared password is proportionate to a paper account and not to a live
  one.** It has no accounts, no rotation and no record of who signed in. If
  this ever fronts real money, replace `auth.py` rather than extending it.

`tailscale serve` instead of `funnel` is the same command without the public
part: an HTTPS URL reachable only by your own devices. If the goal is "view it
on my phone" rather than "show it to someone else", prefer that — it needs no
password at all and there is no secret to leak.

There used to be a public marketing site under `brand/` serving invented
figures, and it was fine there because it read no journal, no broker and no
credential. It has been deleted — one operator, no audience — so this dashboard
is the only surface, and nothing about the old site was ever a precedent for
exposing this one.

---

## Live execution is off, and turning it on is a decision

The unit runs `electrum-bot loop` with **no `--execute` flag**. The bot proposes
orders, runs them through the risk gate, records the outcome, and places nothing.

That is the correct state for a handover. Watch the proposals for a while first
and see whether you agree with them.

Turn it on with the drop-in, **not** by editing the unit:

```sh
sudo mkdir -p /etc/systemd/system/mudhorn-bot.service.d
sudo cp /opt/mudhorn/deploy/systemd/mudhorn-bot-execute.conf \
        /etc/systemd/system/mudhorn-bot.service.d/execute.conf
sudo systemctl daemon-reload
sudo systemctl restart mudhorn-bot
```

Off again is `sudo rm` on that file and the same last two commands. Editing
`ExecStart` in the unit works too, but the unit is what `bootstrap.sh`
reinstalls, so the edit is one deploy away from being silently reverted — and
the direction it reverts in is the one where you believe orders are being
placed and they are not. A drop-in survives, and `systemctl cat mudhorn-bot`
shows the effective `ExecStart` either way.

**What changes is only whether an approved proposal reaches the broker.** Same
context, same model call, same `RiskGate.evaluate` against the same
`config/rules.yaml`, same audit entry. Anything the gate refuses is still
refused. So this is not a way to make a trade fit, and it widens nothing.

It stays paper either way. `ALPACA_PAPER_TRADE=false` is refused by the code,
twice.

What it does buy is a journal with trades in it: fills, slippage against the
quote, resting stops, and an Analytics page with something to measure. Expect
early trades to be instructive rather than profitable. The loop wakes 96 times
a day and Alpha Arena's lesson was that frequency is itself a risk parameter,
so `journalctl -u mudhorn-bot -f` and the Decisions page are worth watching for
the first few sessions rather than checked at the end of the week.

## A console an agent can reach, if you want one

`electrum-bot-console` is an MCP server that runs a small set of operations on
this box — deploy, service status, journal tail, git state, disk and memory, and
the wrapper self-test — so an agent can do the things you would otherwise paste
into a terminal and paste back.

**It is installed disabled and it is the most privileged unit here.** It runs as
**root**, because its job is `deploy/update.sh`, `systemctl` and `journalctl`,
and a console that could not deploy would not be worth its exposure. Published,
it is one bearer token away from a shell on the machine holding your Alpaca
keys. Decide that deliberately.

```sh
sudo /opt/mudhorn/deploy/enable-console.sh            # on, six named operations
sudo /opt/mudhorn/deploy/enable-console.sh --shell    # on, arbitrary argv too
sudo /opt/mudhorn/deploy/enable-console.sh --status   # what is set now
sudo /opt/mudhorn/deploy/enable-console.sh --off      # off
```

It prints the token **once**. Copy it then; it is not shown again, and
`--status` deliberately reports it as set-or-not rather than printing it, the
same rule as the Settings page and the startup banner.

Then publish it and add it as a connector:

```sh
sudo tailscale funnel --bg 8788
tailscale funnel status                # note the https:// URL
```

At claude.ai → Settings → Connectors → Add custom connector, the URL is that
one with `/mcp` on the end, and the header is `Authorization: Bearer <token>`.

### What bounds it, and what does not

- **No trading credential and no route to one.** `console_mcp.py` imports none
  of `broker`, `risk`, `journal`, `models`, `reconcile`, `grants` or
  `mcp_server`; `tests/test_console_mcp.py` parses the AST and fails the build
  if that changes. `place_order` is a different process reached a different way,
  and a console that could also trade would put a command runner and the order
  path behind one secret.
- **Six named operations**, each a fixed argv. `run_command` is not registered
  at all without `--shell`, so a caller cannot see it to try — the same pattern
  as `--execute` and `DASHBOARD_CHAT_TOKEN`.
- **argv lists, never shell strings.** Nothing is word-split or globbed, so
  `&&`, `|`, `>` and `$(...)` are unreachable even with `--shell` on.
- **A token of 32 characters or more, or the server refuses to start.** Unlike
  `DASHBOARD_PASSWORD`, absent is not a supported configuration: a dashboard
  with no password leaks figures, and this runs commands.

**What does not bound it: stopping the service.** A Funnel outlives the unit, so
a URL pointing at a closed port starts working again the moment somebody
restarts it. Take both down:

```sh
sudo /opt/mudhorn/deploy/enable-console.sh --off
sudo tailscale funnel --https=443 off
```

**`bootstrap.sh` never creates the token file**, deliberately. A token generated
by a provisioning script is a token in a provisioning log.

**Worth weighing before you enable it at all:** `claude` installed on this box
does the same job in two minutes with a full shell and no new network exposure.
The console only wins if you want a *specific* agent conversation to reach the
box without one.

## The settings agent can change settings, and that is a grant you install

The Armorer on `/settings` argues about a limit, states what moving it costs in
figures, and then **applies the change herself**. That reverses an earlier
arrangement where she recorded a request and a person applied it later, and the
operator's reason for the reversal was short: *"Settings agent can't edit
settings?? That's broken. That's what the settings agent is for."*

**What did not change is who owns the file.** `config/` is still root-owned and
the web process still runs as `mudhorn`, which still cannot write
`rules.yaml` with its own hands. What was added is the pattern already used for
the chat panel:

```sh
sudo /opt/mudhorn/deploy/enable-forge.sh          # on
sudo /opt/mudhorn/deploy/enable-forge.sh --status # what is set now
sudo /opt/mudhorn/deploy/enable-forge.sh --off    # back to recording only
```

That installs exactly one line, in `/etc/sudoers.d/mudhorn-forge`:

```
mudhorn ALL=(root) NOPASSWD: /opt/mudhorn/deploy/apply-settings.sh
```

**It names the wrapper, never the Python binary.** A rule on
`/opt/mudhorn/.venv/bin/electrum-bot` would permit every subcommand this CLI has
today and every one a future release adds, run as root, with arguments chosen by
whoever is signed in to a dashboard that may answer on the public internet.
`apply-settings.sh` takes **no arguments at all**: it reads `apply <id>` or
`revert <id>` on **stdin**, validates it against a regex before running
anything, and invokes one fixed command against one fixed file.

**This is the only sudo grant here that runs upward.** `run-chat.sh` and
`run-dream.sh` go from `mudhorn` down to `hermes`, an account with no
credentials. This one goes to root, so it is its own script and its own sudoers
file — turning the chat panel on must not quietly hand the service account a way
to edit its own risk limits.

Be exact about what it grants:

| It can | It cannot |
|---|---|
| Move a key listed in `settings_agent.limits_for` | Touch any other line, or any other file |
| Set it to a number | Write arbitrary bytes as root |
| …only if the whole file still loads through `Rules.load` on a staged copy | Leave a config the loop cannot start on |
| Undo it later with `settings-revert <id>` | Escape the record — every change is a row with the reason, the objection and the diff |

**The asymmetry is enforced in code, not in the character file.** A tightening
applies on the first ask. A loosening states the arithmetic consequence and
applies **nothing** until the operator confirms they have read it. `souls/armorer.md`
shapes how that is said; `settings_agent.decide` is what decides it, because a
soul is read off disk at call time and could have been edited.

**Without the sudoers rule everything still works and says less.** The wrapper
sits on disk inert, the agent argues exactly as before, the change is recorded
as a pending request, and both the page and the agent's own briefing say the
file has not moved. That is the fallback, reported plainly — not a silent
failure — and it is also what a laptop deployment gets.

**It also needs the unit to permit `sudo` at all**, which is the same
prerequisite the chat panel has: `NoNewPrivileges` and `RestrictSUIDSGID` both
block it, and `mudhorn-web.service` ships without them for that reason. Watch
for the trap `enable-chat.sh` records — systemd sandboxing is a mount namespace
every child inherits and `sudo` does not escape one, so a `sudo` run from a
console shell can pass while the service is refused. `enable-forge.sh` checks
the unit setting rather than trusting its own shell.

From a root shell, with or without the grant:

```sh
sudo -u mudhorn /opt/mudhorn/.venv/bin/electrum-bot settings-apply      # list pending
sudo /opt/mudhorn/.venv/bin/electrum-bot settings-apply 7 --dry-run     # prove it loads
sudo /opt/mudhorn/.venv/bin/electrum-bot settings-apply 7
sudo /opt/mudhorn/.venv/bin/electrum-bot settings-revert 7              # put it back
```

`settings-revert` restores **the exact text that was on the line**, which is why
the request records it at apply time rather than reconstructing it later. A limit
widened under pressure at 2am is the thing somebody wants undone at 9am, and "it
is in git" is not a route the operator has from the dashboard.

Neither command needs a broker credential, on purpose: they are meant to be run
on a box that may be halfway through a problem, and tightening or undoing a limit
is exactly what somebody wants at that moment.

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

## Pointing the souls at DigitalOcean

Optional. DigitalOcean's **Gradient serverless inference** speaks the Anthropic
Messages API on `https://inference.do-ai.run/v1/messages`, so the three souls
can be pointed at it by exporting two variables — no Python change, no rebuild.
The research and the arithmetic are in `docs/DROPLET_AI.md`.

**Be exact about what moves.** This is that document's phase 1 and nothing else:

| Path | Moves? | Why |
|---|---|---|
| Yoda, Grogu, the Armorer (Hermes) | **Yes** | None of them proposes an order, and three different jobs genuinely want different models |
| `electrum-bot dream` / `confer` | Not yet | Both use server-enforced structured output, which DigitalOcean does not document. A later decision, on its own evidence |
| The trading loop (`claude.propose`) | **Never** | DigitalOcean charges Anthropic's *exact list price*, so it saves nothing — and it would trade a server-enforced schema on the one call that produces order quantities and stop prices |

So `DO_INFERENCE_KEY` in `/opt/mudhorn/.env` is a declaration for later. It
changes no behaviour today and every startup line built from it says
`NOT IN FORCE`, which is the truth rather than a warning.

### 1. The account, by hand

Serverless inference is **prepaid**. A zero balance takes every agent surface
dark at once and nothing on this box can see it, so top it up and set a billing
alert in the same sitting.

Then create a **model access key** in the control panel — AI Platform →
Serverless Inference → *Create model access key* — and scope it to the models
you actually intend to use. Two things about that, both measured rather than
read:

- **Creating one through the API is retired.** `POST /v2/gen-ai/models/api_keys`
  answers `{"id": "gone", "message": "resource retired: ... Go to manage page in
  the control panel"}`. Nothing here may assume a key can be minted
  programmatically; a person makes it in a browser.
- **Not a personal access token.** A PAT controls droplets, DNS and billing, and
  this box also runs an agent with a shell.

### 2. Where the key goes, and why not in `.env`

**Not in `/opt/mudhorn/.env`.** That file is owned by `mudhorn` at mode 600 and
Hermes runs as `hermes`, which cannot read it. That is the user split working,
not a problem to route around: the souls' key is a *second* credential
belonging to the account that spends it, so the agent's environment still holds
nothing that reaches the broker.

One file per Hermes instance — which is what makes per-agent routing real,
because Grogu can run a different model from Yoda and the Armorer:

```sh
sudo -u hermes touch /home/hermes/inference.env          # yoda + the armorer
sudo -u hermes chmod 600 /home/hermes/inference.env
sudo -u hermes touch /home/hermes/dreamer/inference.env  # grogu, if installed
sudo -u hermes chmod 600 /home/hermes/dreamer/inference.env
```

Plain `KEY=value` lines, no `export`:

```
DO_INFERENCE_KEY=<the model access key>
DO_INFERENCE_MODEL=<the serving slug — see step 3>
DO_INFERENCE_BASE_URL=
```

`run-chat.sh` and `run-dream.sh` read their own instance's file and export
`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` for that turn.
No key ever reaches this repository.

### 3. Prove the slug before the first message

**The serving slug is not the Anthropic model id, and it is not what the
catalogue shows you.** The catalogue lists display names and UUIDs; the
inference endpoint wants a slug, and a wrong one fails at the endpoint
mid-conversation. So nothing here guesses it — the wrappers **refuse to run**
with a key and no `DO_INFERENCE_MODEL`, rather than picking a plausible value.

Ask the endpoint, from a laptop rather than the droplet, and read the served
model back out of the answer:

```sh
curl -sS https://inference.do-ai.run/v1/messages \
  -H "x-api-key: $DO_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"<slug>","max_tokens":4,
       "messages":[{"role":"user","content":"Reply with the single word OK."}]}' \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r.get("model") or r)'
```

- A **wrong slug** comes back as an error object, printed whole. That is where a
  bad slug dies: before any soul has used it.
- A **bad key** is `401 {"id": "Unauthorized"}`. Note that a 401 short-circuits
  *before* model lookup, so a 401 proves nothing about the slug — fix the key
  and ask again.
- A **good slug** prints the model that actually served the request. If that is
  not the model you asked for, you are on a router; see below.

Verified live: the endpoint is reachable, `x-api-key` plus
`anthropic-version: 2023-06-01` are the right headers, and the account's
catalogue carries **Claude Opus 5, Sonnet 5, Haiku 4.5 and Fable 5** alongside
the 4.x generation. The model table in `docs/DROPLET_AI.md` predates that and is
out of date; trust the control panel and this check, not the table.

**The catalogue LISTS models the tier cannot call.** Measured 12 Aug 2026: all
ten Anthropic rows appear in `GET /v1/models`, and asking for one returns
**403 `"this model is not available for your subscription tier"`**. Listing is
not entitlement, and the only way to find out is the check above. The open
models — `llama-4-maverick`, `deepseek-v4-pro`, `llama3.3-70b-instruct`,
`deepseek-3.2`, `nemotron-3-ultra-550b` — all answered 200.

### 3b. Set the model in Hermes' own config, because the wrapper cannot

**This step is easy to miss and the whole thing fails without it.** The first
deployment did miss it, and the failure was instructive rather than obvious.

`DO_INFERENCE_MODEL` in `inference.env` does **not** select the model. Neither
does `ANTHROPIC_MODEL`, which the wrapper exports. Hermes reads `model.default`
from its own `$HERMES_HOME/.hermes/config.yaml` — **note the `.hermes/`**, which
is one level lower than it looks and is where the first version of the wrapper's
own check went wrong — and out of the box that is `claude-sonnet-5`:

```sh
sudo -u hermes sed -n '4,6p' /home/hermes/.hermes/config.yaml
# model:
#   default: claude-sonnet-5
#   provider: anthropic
```

Change `default` to the serving slug, and **leave `provider: anthropic`
alone** — that is the wire protocol, and DigitalOcean's `/v1/messages` is
Anthropic-shaped. The base URL is what redirects it.

```sh
sudo -u hermes cp /home/hermes/.hermes/config.yaml \
                  /home/hermes/.hermes/config.yaml.bak-$(date +%s)
sudo -u hermes sed -i '5s|^  default: .*|  default: llama-4-maverick|' \
                  /home/hermes/.hermes/config.yaml
```

**The wrappers now refuse a mismatch** rather than printing a model they cannot
set, so getting this wrong costs a turn and an explanation rather than a quiet
answer from the wrong model. Before that check existed the banner read `model
llama-4-maverick` while Hermes asked for `claude-sonnet-5`; it failed only
because that model happened to be tier-gated, and a slug the account *could*
serve would have answered normally from a model nobody chose.

Note what this means for per-agent routing: Grogu running a different model from
Yoda works through **`HERMES_HOME` pointing at a different config directory**,
which is what `run-dream.sh` already does — not through the environment.

### 3c. Remove every Anthropic credential from Hermes' reach

`$HERMES_HOME/.env` may carry `ANTHROPIC_API_KEY` and `ANTHROPIC_TOKEN` from the
original install, and **they take precedence over what the wrapper exports.**
Symptom is a `401` from DigitalOcean, which is the good direction — the bad one
is an Anthropic credential quietly serving turns the operator believes moved.

```sh
sudo -u hermes cp /home/hermes/.hermes/.env /home/hermes/.hermes/.env.bak-$(date +%s)
sudo -u hermes sed -i 's/^ANTHROPIC_API_KEY=/#ANTHROPIC_API_KEY=/' /home/hermes/.hermes/.env
sudo -u hermes sed -i 's/^ANTHROPIC_TOKEN=/#ANTHROPIC_TOKEN=/'     /home/hermes/.hermes/.env
```

Commented rather than deleted, so the rollback is one character.

**Do NOT touch `/opt/mudhorn/.env`.** That is the trading loop's Anthropic key,
a different user and a different process, and the loop has not moved.

### 4. Do not point a soul at an inference router

The wrappers refuse a `DO_INFERENCE_MODEL` containing `router`, and the reason
is this repository's oldest rule wearing new clothes. A router **falls back to
another model on rate limit**, and `hermes -z` returns the response text and
nothing else — so which model answered a turn is *not observable from this
box at all*. A downgrade nobody can see is worse than a failed call, because a
failure is loud and a downgrade reads exactly like a normal answer.

Pinning a foundation-model slug removes the mechanism rather than watching for
it, which is the only honest option when the observation is impossible. Say
what that does and does not buy: a router's fallback is *documented* absent for
a pinned slug, not *measured* absent.

The same reasoning is why the trading loop is never moving. There, the served
model would have to be read off the response and recorded in the audit event,
and a mismatch treated as a failed cycle — and none of that is worth building
for a provider that charges the same price.

### 5. Confirm it moved, then watch it

**Ask the agent. Do not read a config file.** Same rule as the dropped-toolset
finding in `CLAUDE.md`: the display path and the effective path are different
code. Open the Chat page and ask it something; then check the wrapper's own line
by running one turn by hand, which prints the endpoint it requested on stderr:

```sh
sudo -u hermes /opt/mudhorn/deploy/run-chat.sh <<< 'Reply with the single word OK.'
```

Three things that line is careful about, and they are worth understanding
rather than skipping:

- It claims the endpoint and the configured model were **checked**, and stops
  exactly there — *which model answered* is not visible from this box, because
  `hermes -z` returns the response text and nothing else. A wrapper claiming a
  swap it cannot confirm would be the confident partial answer this project
  exists to prevent.
- If Hermes *ignores* the base URL, the DigitalOcean key reaches Anthropic and
  is refused with a 401 — loud. That is deliberate: the wrapper replaces the
  Anthropic credential rather than leaving one beside a DigitalOcean endpoint,
  because the leftover key is what would let a turn quietly answer from the old
  provider while you believed it had moved.
- If Hermes turns out not to honour these at all, the model is set through
  `hermes model` and `~/.hermes/config.yaml`'s `agent:` block — and that block
  is changed with `deploy/merge-hermes-config.py`, **never** by appending, for
  the duplicate-key reason at the top of `deploy/hermes-config.yaml`.

Move one wrapper at a time — chat first, the dreamer a day later — so a problem
is attributable to one of them.

**Then break it on purpose, because a safety check nobody has watched fire is a
hope.** Point `inference.env` at a model the config does not name and run one
turn; it must refuse with exit 78 and print both values.

```sh
sudo -u hermes sed -i 's/deepseek-v4-pro/llama-4-maverick/' ~hermes/inference.env
echo hi | sudo -u hermes /opt/mudhorn/deploy/run-chat.sh   # expect: refuses, 78
sudo -u hermes sed -i 's/llama-4-maverick/deepseek-v4-pro/' ~hermes/inference.env
```

That is not a hypothetical. **The first version of the check failed this exact
test**, on 13 Aug 2026: it looked in `$HERMES_HOME/config.yaml` rather than
`$HERMES_HOME/.hermes/config.yaml`, found nothing, and `[[ -r ]]` made it skip
in silence — while the banner two lines below went on saying the model had been
checked. The turn ran and answered normally. A config that cannot be read now
refuses, and `tests/test_config.py` pins it by writing no config at all. Run
this after any `bootstrap.sh` that replaces the wrappers.

### Rollback

Blank `DO_INFERENCE_KEY` (or delete the file). The next message picks it up:
`run-chat.sh` execs a fresh Hermes per turn, so **there is no daemon to
restart** and telling anyone to restart one sends them chasing a unit that does
not exist. The wrappers also unset `ANTHROPIC_BASE_URL` on that path, so a
leftover endpoint cannot outlive the key.

A half-configured switch never falls back quietly. A key with no model, a model
naming a router, or a base URL that is not `https://` each **refuse the turn**
with exit 78 and a sentence saying what to fix — which surfaces on the Chat page
as the error, because `HermesBridge` returns the wrapper's stderr on a non-zero
exit. Losing one chat message is the right price for never being told a swap
happened when it did not.

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
sudo /opt/mudhorn/deploy/update.sh
```

That is the whole thing. It pulls, provisions, restarts and verifies, and
**every step is asserted rather than assumed** — which is the difference between
it and the four commands it replaces.

What it checks that a paste cannot:

- **The commit actually moved.** `HEAD` is recorded before and after and
  compared against the upstream ref. A pull that did not land is a hard stop
  *before* anything is provisioned or restarted, so a failed update leaves the
  box exactly as it was rather than half-done.
- **A dirty tree stops it, with the diff printed.** Local changes are a refusal
  unless you pass `--stash`, which sets them aside recoverably
  (`sudo git -C /opt/mudhorn stash list`). It will not silently discard a
  hand-edit — that is a legitimate thing to have been doing.
- **The services came back.** It waits, then checks `is-active`, and prints
  `systemctl status` on the one that did not.
- **The deployed wrapper still refuses a model mismatch.** It puts a
  deliberately mismatched `inference.env` and `.hermes/config.yaml` in a
  temporary directory and runs the real `run-chat.sh` against them with a stub
  binary, requiring exit 78 *and* both values in the message. **It touches
  `/home/hermes` not at all** — mutating an agent's credentials file as a deploy
  step is not a trade worth making for a check. `--skip-verify` turns it off.

It does not switch on `--execute`, the dream timer or the confer timer. Those
are a person's decision and a deploy is not it.

### The four commands it replaces, and why they are no longer the runbook

They are still what it does, and running them by hand still works. What made
them unsafe was that **three of the four cannot tell whether the first one
happened**:

```sh
cd /opt/mudhorn
sudo git pull                                  # STOP. Did it say "Updating ..."?
sudo /opt/mudhorn/deploy/bootstrap.sh
sudo systemctl restart mudhorn-bot mudhorn-web
```

**`bootstrap.sh` replaces the wrappers, so re-run the deliberate-break test
afterwards** — section 5 above, "Confirm it moved". A pull that lands a fix to
`run-chat.sh` is exactly the moment its check is worth watching fire once.

### When the pull refuses

```
error: Your local changes to the following files would be overwritten by merge:
        deploy/run-chat.sh
Aborting
```

Debugging on the box leaves the tree dirty and git will not overwrite it. **Look
before discarding** — a local edit may be the only copy of something:

```sh
sudo git status                # staged AND unstaged, which is the point
sudo git diff                  # UNSTAGED only
sudo git diff --cached         # STAGED only
```

**`git diff` alone is a trap here.** It shows unstaged changes only, so a
*staged* edit prints nothing while the pull keeps refusing — and `git checkout
-- <file>` restores from the **index**, so it dutifully rewrites the modified
version back and changes nothing. Both commands report success and the file is
untouched. Observed exactly this way on 13 Aug 2026.

`stash` does not care which side the change is on, and is recoverable:

```sh
sudo git stash push -u -m "box-local edits, pre-<sha>"
sudo git pull                                  # now says "Updating ..."
sudo git stash list                            # still there if you want it back
```

**This is written down because it cost a deploy.** Two wrappers had been
hand-edited, the pull aborted, `bootstrap.sh` provisioned the old tree and
printed "Provisioned.", and the verification step afterwards reproduced the exact
bug the pull was meant to fix — under a banner claiming the check had run. Every
line of output was true. `bootstrap.sh` now prints the branch and commit it is
provisioning from and shouts when the tree is dirty, because "Provisioned." is
otherwise a claim about a checkout nobody identified.

It does **not** refuse on a dirty tree. A deliberate local edit is a legitimate
thing to be doing on a box, and refusing to provision would be the config-load
validator mistake in a new place: the line has to be impossible to miss, not
impossible to reach.

Two things a pull does **not** carry, both by design:

- **`model.default` lives in `~hermes/.hermes/config.yaml`, not in this repo.**
  It survives a re-merge, because nothing here sets a `model:` block, but it
  would not survive a fresh provision — that lands back on `claude-sonnet-5`,
  and the wrappers would then refuse every turn on the mismatch. Loud, and the
  right direction, but know what you are looking at.
- **`exchange_calendars` is an optional extra** and `bootstrap.sh` installs
  `-e .`, so the droplet keeps the weekday-shaped badges for Tokyo, Sydney and
  Auckland until that becomes `-e ".[calendars]"`. `ClockFace.tracks_holidays`
  reports False and the badge tooltip says so, which is the whole reason the
  dependency is optional — nothing claims to know a holiday it cannot see.

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
