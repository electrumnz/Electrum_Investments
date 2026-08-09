# Transferring ownership

Everything currently sits on the accounts of the person who built this. That was
the fast way to get it running and it is not the end state. This is the checklist
for moving it across.

Do it in one sitting when the new owner has their accounts. Split across two
sessions and you reconnect Vercel twice.

---

## Order matters

Rename first, transfer second, reconnect third. Each step invalidates something
the next one depends on, so going out of order means redoing work.

## 1. Rename the repo — optional, but do it before transferring

`Electrum_Investments` is a working title from before the name was chosen, and
"Electrum" is also a very well known Bitcoin wallet, which is a search collision
for a trading company. `mudhorn-capital` matches the brand.

GitHub redirects old URLs after a rename, so nothing breaks immediately, but
three files hardcode the repository URL and should be updated in the same commit:

- `SETUP.md`, the `git clone` line
- `deploy/systemd/mudhorn-bot.service`, the `Documentation=` line
- `deploy/systemd/mudhorn-web.service`, the same

The `electrum-bot` command name is a separate question and a bigger sweep. It
appears in about forty places across the docs, `pyproject.toml` and the systemd
units. The Python package is `bot`, not `electrum_bot`, so renaming the command
touches no imports and breaks nothing at runtime. Cosmetic either way.

## 2. Transfer the GitHub repository

Settings → General → Danger Zone → **Transfer ownership**.

Commits, branches, issues and pull requests all travel. Old URLs redirect. The
recipient has to accept the transfer before it completes.

**Transfer to a personal account, not a new organisation.** Organisations earn
their keep through teams and permission tiers. For one person they are admin
surface with no return.

## 3. Reconnect Vercel

The transfer **breaks the existing Vercel connection**, because Vercel's GitHub
App is installed on the old owner's account rather than on the repository.

Do not try to transfer the Vercel project. The site is a single static HTML file
with no database, no environment variables and no build step, so there is nothing
worth preserving. The new owner imports the repo into their own Vercel:

- Project Name: `mudhorn-capital`
- Root Directory: **`brand`**
- Framework Preset: Other, no build command
- Production Branch: `main`

Two minutes, and they own it cleanly from the start. The URL changes unless the
old project is deleted first and the name is free again.

---

## What does not travel, and matters

### The Anthropic API key — the only thing that costs money

This is the one to get right. If the new owner keeps using the old owner's key,
**the old owner is paying for the new owner's trading**, indefinitely and
invisibly. It is metered per call.

The new owner needs their own account at
[console.anthropic.com](https://console.anthropic.com), their own key in `.env`,
and the old key revoked afterwards. See `docs/COSTS.md` for what to expect.

### Alpaca keys

`.env` is gitignored, so credentials never travel with a clone. That is
deliberate. A paper account needs only an email address, so the cleanest split is
a fresh paper account on the new owner's email rather than sharing keys.

If the existing paper account is kept, regenerate the keys after handover. Keys
that two people have held are keys neither can reason about.

### `data/journal.db` — the only irreplaceable file

Gitignored, so it does not travel either. It holds every trade, the equity curve
and the persistent stand-down state.

If there is paper history worth keeping, copy it across deliberately:

```sh
sudo -u mudhorn sqlite3 /opt/mudhorn/data/journal.db ".backup '/tmp/journal.db'"
```

Use `.backup` rather than `cp`, which can catch the database mid-write. Leave it
behind and the new box starts with an empty journal and a cleared stand-down
breaker. That is a legitimate choice for a clean start, but it should be a
choice rather than something noticed three weeks later.

### The VPS

Not transferable in any useful sense. The new owner provisions their own box from
`deploy/README.md`, which is two commands, and copies the journal across if they
want the history. Then the old box gets destroyed rather than left running and
billed.

---

## After

- Revoke the old Anthropic key
- Regenerate or retire the old Alpaca paper keys
- Destroy the old VPS
- Update `homepage` on the repo if the Vercel URL changed

The identity page under `brand/` is the only thing published, and it reads no
journal, no broker and no credential. The dashboard stays on `127.0.0.1` behind
Tailscale on whichever machine now runs it. That does not change with ownership.
