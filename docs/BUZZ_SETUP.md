# Buzz + Hermes chat interface

**Decided: run both.** A Buzz channel where the trading agent is a participant
you talk to in plain English, with Hermes underneath carrying memory, scheduled
digests and approval gates. Phone or desktop, no terminal.

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

---

## Install

### 1. Buzz

Desktop builds for macOS, Windows and Linux are on the
[releases page](https://github.com/block/buzz/releases). Install, create your
Nostr identity, and make a channel for the bot.

> Buzz is young. **Mobile has since shipped** on both
> [iOS](https://apps.apple.com/us/app/buzz-chat-with-your-hive/id6779728271) and
> [Android](https://play.google.com/store/apps/details?id=xyz.block.buzz.mobile),
> and they are full clients rather than remote controls needing a laptop open.
> Still rough, with reported message-visibility and notification issues, and
> there was documented onboarding friction at launch. Treat it as promising
> rather than dependable, and keep Claude Code working as the fallback —
> everything the bot does is reachable there without Buzz in the picture at all.

### 2. Hermes

```sh
# Linux, macOS, WSL2
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

```powershell
# Windows, native
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

The installer brings its own uv, Python 3.11, Node.js, ripgrep and ffmpeg, so it
does not disturb an existing toolchain.

### 3. Point Hermes at Claude

```sh
hermes model
```

Pick Anthropic. It supports either a Claude Max subscription via OAuth or a plain
API key — the same key already in your `.env` works.

### 4. Give Hermes the bot's tools

Add both MCP servers to `~/.hermes/config.yaml`, exactly as in
[SETUP.md §5](../SETUP.md): Alpaca's official server restricted to read-only
toolsets, and this repo's `electrum-bot-mcp` as the only order-placing path.

### 5. Connect to Buzz

```sh
hermes gateway setup    # choose Buzz
```

Use a **dedicated Nostr keypair** for this, not the one from your Buzz Desktop
identity. Hermes takes a scoped lock on the relay+pubkey pair, so a separate key
also prevents two Hermes profiles fighting over one Buzz identity.

---

## Check it works

From the Buzz channel:

- *"What's my risk status?"* → equity, cash, open risk, limits
- *"What are the current rules?"* → contents of `config/rules.yaml`
- *"Check a buy of 3 SPY at 580, stop 575, target 590."* → an approved verdict
- *"How are my stops and targets doing?"* → MAE/MFE read from the journal
- *"Buy 1000 shares of GME at market."* → **refused**, with the rules it broke

That last one is the test that matters. If a rule-breaking order goes through,
something is wired wrong — stop and fix it before going further.

---

## Scheduled runs

Hermes has a built-in cron scheduler that can deliver into Buzz:

```
deliver=buzz
```

Useful patterns: a pre-market summary, an end-of-day P&L and audit digest, a
weekly review of which rejected proposals would have worked.

The alternative is [Claude Code Routines](https://www.mindstudio.ai/blog/claude-code-routines-scheduled-tasks-business-automation),
which run in Anthropic's cloud on a Claude Pro plan ($20/month) with your laptop
off. Either works. Hermes keeps everything in one place; Routines avoid running a
gateway process.

---

## Hosting: what has to be running, and where

The short answer to "do I need Vercel": **no.** Nothing here is a public
website. The dashboard, when it exists, binds to `127.0.0.1`.

| Component | Always on? | Where | Cost |
|---|---|---|---|
| Claude Code + MCP servers | No | Your machine | Free |
| Claude Code Routines (scheduled) | No, runs in Anthropic's cloud | Anthropic | Claude Pro, $20/mo |
| **Hermes gateway** | **Yes** | Small VPS, or a PC that stays on | ~$5/mo |
| Buzz relay | Only if self-hosting | Block's hosted relay works | Free |
| Local dashboard | No | Your machine | Free |

**Only the Hermes gateway forces an always-on machine**, and the reason is
obvious once stated: a chat bot that is offline when you message it is not a chat
bot. A $5 Hetzner box is plenty; this process does almost nothing between
messages.

Everything else runs on demand. If you drop the gateway and use Claude Code
directly, the whole stack costs nothing beyond Anthropic API usage.

---

## If Buzz gets in the way

Nothing here is load-bearing. The bot is fully operable from Claude Code and the
CLI. If Buzz breaks after an update, or Hermes' Buzz adapter regresses, drop back
to Claude Code and carry on — you lose the chat convenience, not any capability.
