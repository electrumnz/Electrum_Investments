# Setup

Follow this top to bottom. It takes about 45 minutes, most of which is waiting
for account confirmation emails.

Each step is marked:

- **[human]** — someone has to sit at a keyboard: a signup form, a card, a 2FA app.
- **[auto]** — a command, scriptable, no forms.

Nothing here requires an SSN, an ID document, or a bank account. Alpaca's **paper**
account needs only an email address. Those things are only needed for a *live*
account, which this build deliberately refuses to run against.

---

## 0. Before you start

You need: a computer with a terminal, a browser, and about 45 minutes.
You do **not** need: money, a funded brokerage account, or trading experience.

> **Set expectations first.** This gets you a working paper-trading bot with real
> safety rails. It does not get you a profitable strategy — nobody can hand you
> that. Read the "Reality check" section of `docs/HANDOFF.md` before you decide
> how much of your life to spend on this.

**Running it on a server instead of a laptop?** Do sections 1 and 5 here for the
accounts and the MCP wiring, then follow **`deploy/README.md`** rather than
sections 2 to 4. It provisions a fresh Ubuntu box in one script and runs the bot
as a service that survives a reboot. The two are alternatives, not a sequence.

---

## 1. Accounts

Do these in order. Use **one dedicated email address** for the whole project
(e.g. `yourname.trading@gmail.com`) rather than your personal one — it keeps
credentials easy to hand over later and easy to revoke.

### 1.1 Project email — **[human]**
Create a fresh Gmail/Fastmail/whatever address. Everything below hangs off it.

### 1.2 Password manager — **[human]**
Install [Bitwarden](https://bitwarden.com) (free tier is fine). Put every
credential from here on into it. You will have five or six.

### 1.3 GitHub — **[human]** signup, **[auto]** thereafter
Sign up at [github.com](https://github.com) with the project email. Turn on 2FA.

Then install the CLI and authenticate:
```sh
gh auth login
```

### 1.4 Alpaca paper trading — **[human]**
1. Sign up at [alpaca.markets](https://alpaca.markets) with the project email.
2. Go to **Paper Trading** in the dashboard — not Live.
3. Click **Generate New Key**.
4. Save the **API Key ID** and **Secret Key** to Bitwarden. The secret is shown
   once; if you lose it, regenerate.

Your paper account starts with **$100,000** of virtual money. `config/rules.yaml`
is calibrated to that number.

> No identity verification is involved here. If a form asks for your SSN, you are
> on the live-account flow — back out.

### 1.5 Anthropic API — **[human]**
1. Sign up at [console.anthropic.com](https://console.anthropic.com).
2. Add a payment method and buy a small amount of credit ($5 is plenty to start).
3. Create an API key under **Settings → API Keys**. Save it to Bitwarden.

Budget expectation: roughly **$5–15/month** at the default 15-minute cadence on
Haiku. See `docs/COSTS.md` for the arithmetic.

### 1.6 Claude Pro — **[human]**, optional for now
Only needed if you want **Claude Code Routines**, which run the bot on a schedule
in Anthropic's cloud so your laptop can be off. $20/month. Skip it until the bot
is doing something you want to happen unattended.

---

## 2. Get the code running — **[auto]**

```sh
git clone https://github.com/electrumnz/Electrum_Investments.git
cd Electrum_Investments

python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Confirm the install is sound before touching any credentials:

```sh
.venv/bin/python -m pytest
```

You should see **135 passed**. If not, stop here — something is wrong with the
environment, not with your setup.

---

## 3. Credentials — **[human]** (paste) then **[auto]**

```sh
cp .env.example .env
```

Open `.env` and fill in three values from Bitwarden:

```
ANTHROPIC_API_KEY=sk-ant-...
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
```

Leave `ALPACA_PAPER_TRADE=true`. The bot aborts at startup if it is false, and
`AlpacaBroker` refuses to construct — two independent checks, deliberately.

`.env` is gitignored. Never commit it.

---

## 4. Prove it works — **[auto]**

Offline first, no credentials involved:

```sh
.venv/bin/electrum-bot smoketest --mock
```

Expect a line like:
```json
{"equity": 100000.0, "cash": 100000.0, "positions": 0, "ticks": 0, "event": "connected"}
```
`"ticks": 0` is correct here — the mock broker has no market data. It is proving
the wiring, not the connection.

Now against your real paper account:

```sh
.venv/bin/electrum-bot smoketest
```

This time you should see your paper equity (~$100,000), a non-zero tick count
during market hours, and — if the Anthropic key is set — one Claude call with its
token cost. **No order is placed.**

Check that the paper guard actually works:

```sh
ALPACA_PAPER_TRADE=false .venv/bin/electrum-bot smoketest
echo $?
```

This must print a `live_trading_refused` event and **exit code 2**. If it starts
normally instead, stop and investigate before going any further.

---

## 5. Connect the MCP servers — **[auto]**

Two servers. Alpaca's gives Claude market data and account visibility; ours gives
it the risk gate.

```sh
# Alpaca's official server, restricted to read-only toolsets
claude mcp add alpaca --scope user --transport stdio -- \
  uvx alpaca-mcp-server

# This repo's risk gate
claude mcp add electrum-bot --scope user --transport stdio -- \
  /full/path/to/Electrum_Investments/.venv/bin/electrum-bot-mcp
```

Set the env vars for the Alpaca server in your MCP client config —
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER_TRADE=true`, and
`ALPACA_TOOLSETS` limited to data/account groups.

> **Why restrict Alpaca's toolsets.** Its server can place orders on its own,
> which would bypass the risk gate entirely. Restricting it to read-only leaves
> `place_order` from *this* repo as the only write path, which makes the safety
> rule structural instead of a polite request in a prompt.

Verify in Claude Code:
```
/mcp
```
Both servers should list their tools. Then try: *"What's my risk status?"* and
*"Check a buy of 3 SPY at 580 with a stop at 575 and a target at 590."*

---

## 6. Watch it before you let it trade — **[auto]**

```sh
.venv/bin/electrum-bot loop
```

This proposes and vets continuously but **places nothing**. Let it run for a few
sessions. Read `audit/<date>.jsonl` and decide whether you actually agree with
what it wanted to do.

When you do, and only then:

```sh
.venv/bin/electrum-bot loop --execute
```

Still paper money. Still every order through the gate.

---

## 7. Buzz + Hermes chat interface — optional

See **[docs/HERMES_SETUP.md](docs/HERMES_SETUP.md)**. It has a security caveat that
matters for anything holding trading tools — read it before installing, not after.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `LiveTradingRefused` at startup | `ALPACA_PAPER_TRADE` is not `true` | Set it back. This guard is working as designed. |
| `ALPACA_API_KEY and ALPACA_SECRET_KEY must be set` | `.env` missing or unfilled | `cp .env.example .env` and fill it in. |
| Smoketest shows equity but no quotes | Market closed, or symbol not on your data plan | Normal outside US market hours. Crypto quotes work 24/7. |
| Everything rejected with "outside the allowed trading sessions" | Running outside 14:00–21:00 UTC | Expected. Adjust `sessions_utc` in `config/rules.yaml` if your hours differ. |
| `no module named alpaca` | venv not used | Prefix commands with `.venv/bin/`. |
| Claude Code doesn't see the tools | Wrong path in `claude mcp add` | Use the absolute path to `.venv/bin/electrum-bot-mcp`. |

---

## What you have when this is done

- A paper-trading bot with a deterministic risk gate that the model cannot argue with
- Plain-English control of it from Claude Code
- An append-only audit log of every decision and its reasoning
- Zero real money at risk, and no path to risking any without deliberate work

What you do **not** have is a strategy. That is the hard part, it is yours, and
`docs/HANDOFF.md` is where to start.
