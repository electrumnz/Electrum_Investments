# Buzz + Hermes chat interface

Optional. Gives you a chat channel where the trading agent is a participant you
can talk to in plain English, from your phone or desktop, instead of a terminal.

**Read the security section before installing.** There is a real caveat that
specifically affects agents with trading tools attached.

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

Together: your friend chats with the bot in a Buzz channel; Hermes carries the
memory and the approval gates; the bot's MCP server still enforces the risk rules.

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

> Buzz is at 0.4.x. Mobile apps have not shipped, and there was documented
> onboarding friction at launch. Treat it as promising rather than dependable, and
> keep Claude Code working as your fallback interface — everything the bot does is
> reachable from there without Buzz in the picture at all.

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

- *"What's my risk status?"* → equity, cash, limits, day-trade count
- *"What are the current rules?"* → contents of `config/rules.yaml`
- *"Check a buy of 10 SPY at 580, stop 575, target 590."* → an approved verdict
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

## If Buzz gets in the way

Nothing here is load-bearing. The bot is fully operable from Claude Code and the
CLI. If Buzz breaks after an update, or Hermes' Buzz adapter regresses, drop back
to Claude Code and carry on — you lose the chat convenience, not any capability.
