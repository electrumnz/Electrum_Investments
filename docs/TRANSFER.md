# Handing it over

Josh owns nothing yet. Every account is created and held by the person who built
this, under a Mudhorn Capital identity, and handed over as a set. That makes the
handover a **credential transfer plus a payment-method swap**, not six separate
account migrations.

That is the easier shape, and it is worth protecting: the closer everything sits
to a single brand identity beforehand, the less there is to do on the day.

---

## The two things that actually cost money

Everything else on the list is free, so the handover event is narrower than it
looks:

| | Cost | Why it matters |
|---|---|---|
| **Anthropic Console** | metered, ~$3–15/mo | Every decision the bot makes bills here |
| **VPS** (DigitalOcean) | $12/mo flat | The always-on box |

Swap the card on those two and the money has moved. Nothing else has a payment
method attached.

**Until then, the builder is paying both.** Small, but it starts the day the VPS
is provisioned rather than the day Josh takes over, so provisioning early has a
running cost.

---

## Where each account sits

Account handles are deliberately left out below. This repository may be public,
and a published list of which account holds what is free targeting material for
a plausible-looking "support" email. The live mapping belongs in the password
manager, next to the credentials.

| Service | Held under | At handover |
|---|---|---|
| GitHub, brand account | brand identity | Hand over credentials |
| GitHub repo | **a personal account** | Move first — see below |
| Vercel project | **a personal team** | Move first — see below |
| Tailscale tailnet | brand identity | Hand over credentials |
| Alpaca (paper) | project Gmail | Hand over credentials, then rotate keys |
| Anthropic Console | project Gmail | Hand over credentials, swap card, rotate key |
| DigitalOcean | builder's account | Hand over credentials, swap card |

Two rows are the odd ones out.

### Move the repo to the brand account

The repo sits on a personal account, under a working title chosen before the
name existed. Both are worth fixing in one go: rename to `mudhorn-capital`, then
transfer to the brand account (Settings → General → Danger Zone → Transfer
ownership). Commits, branches and issues travel; old URLs redirect.

Three files hardcode the repository URL and should be updated in the same commit:

- `SETUP.md`, the `git clone` line
- `deploy/systemd/mudhorn-bot.service`, the `Documentation=` line
- `deploy/systemd/mudhorn-web.service`, the same

Do this **before** wiring anything else to the repo, because the transfer breaks
Vercel's connection (its GitHub App is installed per account, not per
repository).

### Re-import Vercel afterwards

The site is a single static file with no database, environment variables or
build step, so there is nothing worth preserving. Import the repo again from the
brand account's side:

- Project Name: `mudhorn-capital`
- Root Directory: **`brand`**
- Framework Preset: Other, no build command
- Production Branch: `main`

Two minutes. Delete the old project first if you want the same URL back.

### Do not create a GitHub organisation

Organisations earn their keep through teams and permission tiers. For one person
they are admin surface with no return, and they make the credential handover
harder rather than easier.

---

## On the day

1. **Hand over the password manager entry** covering every account above
2. **Swap the payment method** on Anthropic Console and DigitalOcean
3. **Rotate the Anthropic API key** — the old one is in the builder's `.env` and
   on the VPS, and it bills to whoever's card is now attached. Generate a new
   one, update `/opt/mudhorn/.env`, revoke the old
4. **Regenerate the Alpaca paper keys.** Keys two people have held are keys
   neither can reason about. Regenerating costs nothing on a paper account
5. **Swap the SSH key on the droplet.** The key authorised at creation belongs
   to the builder's machine, and it grants root on the box holding the broker
   credentials. Josh adds his own public key to `/root/.ssh/authorized_keys`,
   confirms he can log in with it **in a second terminal while the first is
   still open**, then removes the builder's line. Delete it from the
   DigitalOcean account too, or it reappears on the next droplet
6. **Change the passwords** on the accounts themselves, once he has them

Steps 3 to 5 are the ones people skip. Each is two minutes, and each closes a
door that is otherwise left open indefinitely. Step 5 is the one that matters
most: an API key can be revoked from a web console later, but an SSH key nobody
remembers granting is root access that outlives every other credential on the
list.

---

## What does not travel automatically

### `.env` is gitignored, on purpose

Credentials never travel with a clone. A fresh clone starts with no keys and the
bot refuses to run until they are filled in. That is correct — it just means the
`.env` on the VPS is the only copy, and it needs recreating anywhere else the
bot runs.

### `data/journal.db` is the only irreplaceable file

Also gitignored. It holds every trade, the equity curve and the persistent
stand-down state. If there is paper history worth keeping, copy it deliberately:

```sh
sudo -u mudhorn sqlite3 /opt/mudhorn/data/journal.db ".backup '/tmp/journal.db'"
```

`.backup` rather than `cp`, which can catch the database mid-write.

Leaving it behind is a legitimate choice — a clean journal and a cleared
stand-down breaker is a reasonable place for a new owner to start. It should
just be a decision rather than something noticed three weeks later.

### The VPS itself

Transferring a droplet between DigitalOcean accounts is more trouble than
rebuilding it. `deploy/README.md` provisions a fresh box in two commands; copy
the journal across and destroy the old one so it stops billing.

---

## What stays true regardless of owner

`brand/` is published at https://mudhorn-capital.vercel.app and is safe there:
static, and it reads no journal, no broker and no credential.

**The dashboard is not.** It renders account equity, open positions and realised
P&L, and it has no login *because* it binds to `127.0.0.1`. Remote access is
Tailscale. That does not change with ownership, and it is written into
`CLAUDE.md` so a future session cannot quietly undo it.
