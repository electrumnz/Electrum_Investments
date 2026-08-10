# Upgrading a running box

`deploy/README.md` provisions a fresh droplet. This is the other job: moving a
box that is **already running, already holding a position, and already carrying
data nobody can regenerate** onto newer code.

The difference matters. A fresh install has nothing to lose and no migration to
run. An upgrade has both.

---

## What is actually at risk

Two SQLite files, and only one of them is irreplaceable.

- **`data/journal.db`** — the only irreplaceable file on the box. It is the sole
  source of `open_risk_usd`, because Alpaca keeps stop-losses as separate orders
  and cannot report what a position was *meant* to risk. Lose it and the 2% cap
  has nothing to count.
- **`data/dreams.db`** — speculative notes plus, now, the **adoption records
  that grant symbol permissions**. Losing it costs the dreams and silently
  narrows what may be traded back to `config/rules.yaml`. That is the safe
  direction, which is why it is still not backed up by
  `deploy/backup-journal.sh`.
- **`data/insight.db`** — derived. Delete it freely; `electrum-bot reindex`
  rebuilds it from the audit log.
- **`audit/*.jsonl`** — append-only and never migrated. Nothing here touches it.

---

## Migrations that run on this upgrade

Both are **additive** — `ALTER TABLE ... ADD COLUMN`, guarded by
`PRAGMA table_info`, idempotent, applied automatically the first time the new
code opens each file. Nothing is dropped and nothing is rebuilt.

| File | Change |
|---|---|
| `journal.db` | `trades.dream_id INTEGER` — which adopted dream permitted a trade |
| `dreams.db` | vault columns on `dreams`, plus new `dream_messages` and `adoptions` tables |

**Additive is why rollback is survivable.** Older code selects the columns it
knows by name and ignores the rest, so a database that has been migrated
forward still opens under the previous release. That is a property of *these*
migrations, not a general guarantee — a future one that rebuilds a table (as
`_drop_planned_target_not_null` does) breaks it, and this table needs updating
when that happens.

---

## The order, and why

```sh
# 1. On the box, as root.
cd /opt/mudhorn

# 2. Snapshot BEFORE anything else. Not cp -- the bot may be mid-write and
#    SQLite keeps a WAL and an shm alongside the file, so a copy taken between
#    two writes opens fine and reports corruption later.
./deploy/backup-journal.sh
sqlite3 data/dreams.db ".timeout 5000" ".backup /root/dreams-pre-upgrade.db"
sqlite3 /root/dreams-pre-upgrade.db "PRAGMA integrity_check;"   # must say ok

# 3. Stop the writers. The web unit only reads, but it holds a poller and a
#    broker session, so stop it too rather than reasoning about whether it
#    matters.
systemctl stop mudhorn-bot mudhorn-web

# 4. Take the code.
git fetch origin
git checkout <branch-or-tag>
git pull --ff-only

# 5. Dependencies, then the migrations, which run on first open.
./deploy/bootstrap.sh            # idempotent; installs new deps, rewrites units
sudo -u mudhorn .venv/bin/electrum-bot smoketest --mock

# 6. Back up.
systemctl start mudhorn-bot mudhorn-web
systemctl status mudhorn-bot mudhorn-web --no-pager
```

**`bootstrap.sh` rewrites the systemd units**, which is why
`mudhorn-bot-execute.conf` is a drop-in rather than an edit to
`mudhorn-bot.service` — an edit would be overwritten here and `--execute` would
silently switch off. Check the drop-in survived if you rely on it.

---

## Verify, and do not skip this

A green suite says nothing about the deployed thing. This project has been
caught four separate times that way — an ignored directory, a 401 on `/live`, a
CSS collision, and a journal schema that could not store what the models
allowed. Look at the box.

```sh
# The migrations actually applied, rather than "the service started".
sqlite3 data/journal.db "PRAGMA table_info(trades);" | grep dream_id
sqlite3 data/dreams.db  ".tables"                    # dream_messages, adoptions

# The position is still journalled and its risk still reads.
sqlite3 data/journal.db \
  "SELECT id, symbol, direction, qty, entry_price, planned_stop FROM trades WHERE exit_time IS NULL;"

# The loop is running and saying what it can and cannot see.
journalctl -u mudhorn-bot -n 40 --no-pager | grep cycle_complete
```

That last line should carry `calendar_degraded`, `symbols_without_history`,
`stops_unchecked` and now `granted_symbols` on every cycle. **A zero there is a
stated fact; an absent field is an outage.** That distinction is the whole
reason those counters are printed even when they are empty.

Then open the dashboard and look at it:

- the **Board** shows equity, positions and orders, and the `as at` stamp
  describes the READING rather than the render;
- the resting stop leg shows **its trigger price**, not `market` and not a
  blank — that was the point of `WorkingOrder.stop_price`;
- **Settings** reports credentials as configured or not, and renders none.

---

## Rolling back

```sh
systemctl stop mudhorn-bot mudhorn-web
git checkout <previous-commit>
./deploy/bootstrap.sh
systemctl start mudhorn-bot mudhorn-web
```

The databases do **not** need restoring for this upgrade, because both
migrations are additive (see above). Restore from the snapshot only if
something has actually corrupted:

```sh
systemctl stop mudhorn-bot mudhorn-web
cp /root/mudhorn-backups/journal-<stamp>.db data/journal.db
```

**A restored journal is a journal that has forgotten recent trades.** Run
`electrum-bot smoketest` and reconcile against the broker before letting the
loop run again — `reconcile` will close anything the journal thinks is open
that the broker no longer holds, which is right, but it will do it at estimated
prices and those propagate into every downstream metric.

---

## What this runbook cannot do for you

**It cannot cancel an order.** There is no `cancel_order` on the `Broker`
protocol, so a resting order can be abandoned but never withdrawn from here.
That is `TODO.md` item 5.

**It cannot tell you whether `--execute` is on** beyond checking the drop-in
exists. Whether the loop is placing orders is a question for
`systemctl cat mudhorn-bot` and the logs, not for this file.

**It cannot verify the Tailscale link will still be there next week.** Node
keys expire, the Funnel stops serving, and the bot carries on trading normally
with the only symptom a URL that no longer answers. `tailscale status` and the
dashboard's own banner are the check; see `src/bot/tailnet.py`.
