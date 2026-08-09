# Hermes agent runtime (and Buzz, later)

**Hermes now, Buzz deferred.** Hermes is the runtime: memory across sessions, a
cron scheduler, approval gates, and MCP client support. It is worth having on
its own — everything except the phone surface works from the CLI, and adding a
chat platform afterwards is one command.

**Buzz is deferred, not dropped.** The chat surface can come later. Telegram,
WhatsApp and Signal all require a phone number, which for a handover means
Josh's, which means it cannot be set up in advance. Buzz needs no phone and no
account at all — identity is a Nostr keypair — so it remains the intended
surface. Discord is the fallback if Buzz's rough edges bite: email signup, no
phone, and v0.19.0 added an admin-only gate on approval buttons.

**Read the security section before installing.** There is a real caveat that
specifically affects agents with trading tools attached, and it rules out the
two easiest setup paths.

## They are two different layers, not two options

- **Buzz** is the **chat surface**. Where you type.
- **Hermes** is the **agent runtime**. Memory across sessions, a cron scheduler,
  approval gates, MCP client support.

Either works alone. Buzz can drive Claude Code directly with no Hermes; Hermes
reaches a phone through Telegram with no Buzz. Running both is the most capable
arrangement and also the one with the most moving parts, which is the trade being
made here deliberately.

If it ever feels like too much, **drop Buzz, keep Hermes on Telegram**. That
loses the workspace but keeps everything that actually does work: memory,
digests and approvals.

---

## What these are

**[Buzz](https://github.com/block/buzz)** — Block's open-source workspace,
released 21 July 2026 under Apache-2.0. Looks like Slack, except AI agents are
first-class members: every participant, human or agent, is a Nostr keypair rather
than a platform account. Self-hostable, or use Block's hosted relay.

**[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — Nous Research's
agent runtime, MIT-licensed. Adds what a bare chat integration lacks: persistent
memory across sessions, a skill-learning loop, approval gates on tool calls, a
cron scheduler, and MCP client support. It speaks Telegram, Discord, Slack,
WhatsApp, Signal, CLI and Buzz from one gateway process.

Together: you chat with the bot in a Buzz channel, Hermes carries the memory and
the approval gates, and the bot's MCP server still enforces the risk rules
underneath. Nothing about this arrangement can talk `risk.py` into anything.

---

## Security caveat — read this first

Hermes offers [three ways to connect to Buzz](https://hermes-agent.nousresearch.com/docs/integrations/buzz).
They are **not** equally safe for this use case.

| Path | Setup effort | Approval gates | Use for a trading bot? |
|---|---|---|---|
| ① Desktop runtime (Buzz spawns Hermes) | Zero config | **Bypassed** | **No** |
| ② Relay bridge (`buzz-acp`) | Moderate | **Bypassed** | **No** |
| ③ Native gateway (`hermes gateway setup`) | Moderate | **Intact** | **Yes** |

In ACP mode — paths ① and ② — **Buzz auto-approves tool permission requests.**
Hermes' own docs are explicit that setting `approvals.mode: manual` does not help,
because Buzz approves the request anyway, and that `platform_toolsets.acp` cannot
be used to drop the `terminal` toolset. In practice that means anyone who can post
in the channel can make the agent run shell commands.

For a general assistant that is a manageable trade-off. For an agent wired to a
brokerage account it is not, even a paper one — the same channel is how you would
later reach a live account.

**So: use path ③, the native gateway.** It costs one extra setup command and keeps
Hermes' approvals, memory and session management working. It also wants its own
dedicated Nostr keypair, which is good hygiene regardless.

Whichever path you pick, keep the agent **owner-only**. Hermes' docs recommend
this and it is doubly true here.

> **Re-checked against Hermes v0.20.0 (2026-08-03).** Buzz is now a *bundled*
> platform plugin with native WebSocket transport and NIP-42 auth, so path ③ is
> better supported than when this was written — but all three paths still exist
> and the ACP auto-approval caveat still applies to ① and ②. The table above
> stands.

### The more important caveat, added in v0.19.0: approvals are now LLM-judged

Hermes' **smart approvals are enabled by default**. Rather than asking you about
every flagged command, *an LLM reviewer assesses it independently* and decides.

Read that against the first line of `CLAUDE.md`. `src/bot/risk.py` is
deterministic Python **because it cannot be persuaded** — that is the entire
architectural bet of this project, taken directly from Alpha Arena, where the
damage came from confident, fluent, wrong models with nothing between them and
the account.

An LLM deciding whether a shell command is safe, on the box holding the broker
credentials, puts a persuadable thing back in the one position the design exists
to keep it out of. It is the same mistake in a different place.

**So on this deployment, smart approvals must not be the last line.** Two things
to configure at install time:

1. **Turn smart approvals off**, so a flagged command waits for a human.
2. **Add deterministic deny rules** for anything that touches credentials or the
   broker. Hermes' deny rules "block commands even under yolo mode", which makes
   them the only layer in the approvals system that behaves like `risk.py` does:
   it refuses, and nothing talks it round.

Also worth knowing, same release: `hermes approvals suggest` mines your approval
history into proposed allowlists. Useful on a general assistant. **Do not run it
here** — an allowlist generated from what you happened to approve is precisely
the ratchet this setup should not have.

> The behaviour above is from the v0.19.0 and v0.20.0 release notes. The exact
> config keys were not verifiable when this was written, so check them against
> the live configuration docs when you install, rather than trusting a key name
> reproduced here from memory.

---

## Install, on the droplet

Hermes goes on the same box as the bot. It is already always-on, already has the
MCP server, and already holds the credentials.

### 1. The user question, which is the whole security design

**Hermes cannot drop its `terminal` toolset.** Whatever else is configured, the
agent can run shell commands. So the question is not "can it be trusted with a
shell" but "what does a shell on that box actually get it".

Run it as `root` and the answer is everything. Run it as `mudhorn` — the bot's
own service account — and the answer is the broker credentials in `.env` and
write access to `data/journal.db`. That second one matters more than it looks:
the journal is where the consecutive-loss stand-down persists, and
`journal.py` keeps it in SQLite specifically so that *restarting the process
does not clear it*. An agent that can edit that file can clear its own
stand-down, which defeats the rule entirely.

So Hermes gets **its own unprivileged user with no access to either**:

```sh
sudo useradd --system --create-home --shell /bin/bash hermes
```

It reads no `.env`, and cannot touch `data/` or `audit/`.

### 2. Reaching the bot without holding its keys

That leaves a problem: `electrum-bot-mcp` needs the credentials and the journal,
and Hermes spawns it as a subprocess, which would normally inherit Hermes' user.

Solve it with one sudoers rule so the MCP server — and only the MCP server —
runs as the account that does have access:

```sh
sudo tee /etc/sudoers.d/hermes-mcp >/dev/null <<'EOF'
hermes ALL=(mudhorn) NOPASSWD: /opt/mudhorn/.venv/bin/electrum-bot-mcp
EOF
sudo chmod 440 /etc/sudoers.d/hermes-mcp
sudo visudo -c
```

No wildcards, one exact binary path, and `/opt/mudhorn/.venv` is root-owned so
`hermes` cannot swap the binary out from under the rule. Run `visudo -c` before
trusting it; a malformed sudoers file can lock you out of `sudo` entirely.

**This is the property worth having.** The agent reaches the broker *only*
through the MCP tool surface, and every order-placing tool there runs
`RiskGate.evaluate` first. Its shell is not an order path, because the shell
user has no credentials to place one with. `CLAUDE.md` says never add an order
path that skips the gate; this arranges things so the obvious one never existed.

### 3. Hermes itself

```sh
sudo -u hermes -i
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

The installer brings its own uv, Python 3.11, Node.js, ripgrep and ffmpeg, so it
does not disturb the bot's virtualenv.

### 4. Point it at Claude

```sh
hermes model      # choose Anthropic
```

**Use a separate Anthropic API key from the bot's.** Same console, second key.
The bot's spend is a known ~$8/month and its cost per cycle is recorded in
`audit/`; mixing an interactive agent's usage into that makes both
unattributable, and it means revoking one does not revoke the other.

### 5. Give it the bot's tools

In Hermes' MCP configuration, both servers — note the `sudo -u mudhorn`:

```yaml
mcp_servers:
  electrum-bot:
    command: sudo
    args: ["-u", "mudhorn", "/opt/mudhorn/.venv/bin/electrum-bot-mcp"]
  alpaca:
    # Alpaca's official server, restricted to read-only toolsets.
    # See SETUP.md section 5 for the ALPACA_TOOLSETS value.
```

Since v0.20.0, MCP servers start lazily from a fingerprint-keyed schema cache
rather than all booting at session start, so a server that is slow or down no
longer delays every session.

### 6. Approvals — do not skip this

Covered in the security section above and it is the part most likely to be
waved through. Smart approvals are **on by default** and hand the decision to an
LLM reviewer. Turn them off, and add deny rules for anything touching
credentials or the broker.

> Config key names were not verifiable when this was written. Take them from the
> live configuration docs rather than guessing — a deny rule with a wrong key
> silently matches nothing, which is worse than no rule because it looks
> configured.

### 7. Run it as a service

Once it works interactively, the gateway wants the same treatment as the other
two units, so it survives a reboot. Model it on
`deploy/systemd/mudhorn-bot.service`: `User=hermes`, `Restart=on-failure`,
`NoNewPrivileges` **left off** — the sudoers rule above needs it — and the rest
of the hardening kept.

---

## Check it works

From the Hermes CLI (`hermes` as the `hermes` user), or later from a chat channel:

- *"What's my risk status?"* → equity, cash, open risk, limits
- *"What are the current rules?"* → contents of `config/rules.yaml`
- *"Check a buy of 3 SPY at 580, stop 575, target 590."* → an approved verdict
- *"How are my stops and targets doing?"* → MAE/MFE read from the journal
- *"Buy 1000 shares of GME at market."* → **refused**, with the rules it broke

That last one is the test that matters. If a rule-breaking order goes through,
something is wired wrong — stop and fix it before going further.

---

## Scheduled runs

Hermes has a built-in cron scheduler. Without a chat surface it can still write
a digest to a file or the journal log; once Buzz is connected, `deliver=buzz`
sends it to the channel.

Useful patterns: a pre-market summary, an end-of-day P&L and audit digest, a
weekly review of which rejected proposals would have worked.

The alternative is [Claude Code Routines](https://www.mindstudio.ai/blog/claude-code-routines-scheduled-tasks-business-automation),
which run in Anthropic's cloud on a Claude Pro plan ($20/month) with your laptop
off. Either works. Hermes keeps everything in one place; Routines avoid running a
gateway process.

---

## Hosting: what has to be running, and where

The short answer to "do I need Vercel": **no.** Nothing here is a public
website. The dashboard binds to `127.0.0.1`.

| Component | Always on? | Where | Cost |
|---|---|---|---|
| Claude Code + MCP servers | No | Your machine | Free |
| **Bot loop + dashboard** | **Yes** | The droplet | included above |
| Claude Code Routines (scheduled) | No, runs in Anthropic's cloud | Anthropic | Claude Pro, $20/mo |
| **Hermes gateway** | **Yes** | The droplet that already runs the bot | $12/mo, already paid |
| Buzz relay | Only if self-hosting | Block's hosted relay works | Free |
| Local dashboard | No | Your machine | Free |

**Only the Hermes gateway forces an always-on machine**, and the reason is
obvious once stated: a chat bot that is offline when you message it is not a chat
bot. This process does almost nothing between messages, so the smallest box on
offer is plenty.

Everything else runs on demand. If you drop the gateway and use Claude Code
directly, the whole stack costs nothing beyond Anthropic API usage.

### One provider, not two

A reasonable assumption is that this needs a web host *and* a VPS. It does not,
because **there is no website**. One small box runs the gateway, the dashboard
and the SQLite journal together.

**Vercel cannot host this**, and that is a capability mismatch rather than a
preference. It runs serverless functions and static sites: no always-on
processes, execution time limits, and no persistent local filesystem. The
gateway is long-running and the journal is a file on disk. Both are precisely
what Vercel does not do.

**Decided: a VPS, not a home machine.** Running it on Josh's own PC was the
cheapest correct answer and is no longer on the table, so the free option below
is recorded only as the road not taken.

| Option | Cost | Why |
|---|---|---|
| **DigitalOcean, 2 GB** | $12/mo | The pick. Friendliest console and documentation of any provider, US regions, and the headroom question stops being a question |
| DigitalOcean, 1 GB | $6/mo | Workable, but see the sizing note below. Add swap if you take it |
| Hetzner | ~€4/mo | Roughly half the price and the plainer console is no real obstacle, but its cost-optimised line is listed in Frankfurt, Nuremberg and Helsinki, so a US operator is reaching across the Atlantic |
| Fly.io | ~$5/mo | Works (persistent volumes, always-on machines) but is container-oriented, so you learn deploys to save nothing |
| ~~Josh's own PC~~ | Free | Ruled out. Fine in principle, but only if the machine is reliable and actually stays on |

**Sizing, because $6 is tempting and 1 GB is tighter than it looks.** The box
does not run one process, it runs five: the bot loop, the dashboard, the Hermes
gateway (Node plus its own bundled Python), and one child process per MCP server
(Alpaca's and this repo's). On a 1 GB droplet, minus the OS, that is close enough
to the ceiling to need a swap file. CPU genuinely does not matter here, because
a 15-minute cadence leaves the machine idle almost all of the time. Buy RAM, not
cores.

Prices checked on 9 August 2026; DigitalOcean's were read from its pricing page,
Hetzner's were not (the figures are rendered client-side) so treat that row as
approximate.

### Reaching the dashboard when away from the desk

The dashboard binds to `127.0.0.1`, so on its own it is only reachable from the
machine running it. That is deliberate and it is why no login exists.

Day to day this does not matter, because **the phone interface is chat, not the
dashboard**. Buzz and Telegram both reach the gateway from anywhere. The
dashboard is for sitting down and studying an equity curve, which is a desk
activity.

When remote access to the dashboard is genuinely wanted, use
**[Tailscale](https://tailscale.com/)** (free for personal use, about five
minutes to set up). It is a private mesh network, so the dashboard keeps binding
to `127.0.0.1` and the phone reaches it over the private link. Nothing is
published.

Do **not** simply put the dashboard on a public URL. The moment it is publicly
reachable it needs real authentication, and the reason there is none today is
precisely that it is not.

---

## If Buzz gets in the way

Nothing here is load-bearing. The bot is fully operable from Claude Code and the
CLI. If Buzz breaks after an update, or Hermes' Buzz adapter regresses, drop back
to Claude Code and carry on — you lose the chat convenience, not any capability.
