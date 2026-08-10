#!/usr/bin/env bash
#
# Snapshot data/journal.db and audit/*.jsonl. Run hourly by
# mudhorn-backup.timer.
#
#   /opt/mudhorn/deploy/backup-journal.sh
#
# ## Why this exists
#
# journal.db is the only irreplaceable file on the box. Every trade, the entry
# and exit that make an R-multiple mean anything, the excursions, and the
# persistent stand-down state all live in it and nowhere else. The broker cannot
# reproduce it: Alpaca holds stop-losses as separate orders, so the planned stop
# on a position (and therefore open risk, and therefore the 2% cap) exists only
# here. Losing it does not lose history, it loses the ability to enforce a limit.
#
# The audit log is the second irreplaceable thing, and it was left out of this
# script for longer than it should have been. It is the ONLY record of a
# rejected proposal — one the gate refuses never becomes a trade, so it reaches
# neither the journal nor the broker — and since `news_history.py` it is also
# the only record of what the loop was shown: every headline, every watched
# post, every earnings window. Both of those answer "why did it do that", which
# is the question the journal cannot answer at all.
#
# ## Why sqlite3 .backup and not cp
#
# cp reads the file while the bot may be part-way through a write. SQLite is
# writing a WAL and a shm alongside the database, and a copy taken between two
# of those writes is a file that opens and then reports corruption later, at
# whatever moment something reads the wrong page. The online backup API used by
# `.backup` takes a read lock, copies page by page, and restarts if a writer
# gets in the way, so what lands is a consistent database rather than a
# consistent-looking one.
#
# The distinction matters more than usual here, because a corrupt backup fails
# silently until the day it is needed. That is why every snapshot is opened and
# integrity-checked before it is kept.
#
# ## Why the audit log IS just copied, which inverts the rule above
#
# Worth stating plainly, because the section above is the loudest thing in this
# file and pattern-matching on it here would be wrong.
#
# The reasoning for `.backup` is about SQLite specifically: a database with a
# WAL and an shm has state spread across three files, so a copy taken between
# two writes is internally inconsistent. An audit file is append-only UTF-8
# text with one JSON object per line. There is no second file, no page to land
# half-written, and the worst a mid-write copy can produce is a truncated final
# line — which `audit._parse` already tolerates by design, counting it as
# malformed rather than raising. `.backup` has no meaning for a text file, and
# there is nothing here for it to protect against.
#
# What IS worth doing is verifying the compressed result, for the same reason
# the journal snapshot is integrity-checked: a backup nobody has opened is a
# hope. `gzip -t` is the counterpart.
#
# ## What this does and does not protect against
#
# `data/insight.db` is deliberately absent from all of this, and adding it
# would be a mistake rather than an improvement. It is a derived index over the
# audit log, rebuilt by `electrum-bot reindex` in well under a second, so it is
# not irreplaceable — and restoring a stale copy of it over a current one is a
# way to make queries quietly answer from last week.
#
# Everything here lands in $BACKUP_DIR on the same droplet, so it covers an
# accidental delete, a bad deploy and a truncated file. It does NOT cover
# losing the droplet. Copying backups/ off the box is deliberately not
# automated — it needs a destination and a credential, and a credential that
# can write to your backups should not sit on the machine running the trading
# loop. That trade-off is the operator's to make, not this script's.
#
# ## Restoring
#
#   sudo systemctl stop mudhorn-bot mudhorn-web
#   gunzip -c /opt/mudhorn/backups/daily/journal-2026-08-09.db.gz \
#     > /opt/mudhorn/data/journal.db
#   sudo chown mudhorn:mudhorn /opt/mudhorn/data/journal.db
#   sudo -u mudhorn sqlite3 /opt/mudhorn/data/journal.db 'PRAGMA integrity_check;'
#   sudo systemctl start mudhorn-bot mudhorn-web
#
# Stopping the bot first is not optional. Restoring underneath a running process
# leaves it holding a file handle to the database it thinks it has, and the
# stand-down state it reloads afterwards is anyone's guess.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mudhorn}"
DB="${DB:-$APP_DIR/data/journal.db}"
AUDIT_DIR="${AUDIT_DIR:-$APP_DIR/audit}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"

# Hourly snapshots are kept for a few days; the first snapshot of each day is
# also linked into daily/ and kept for a quarter. Hard links, so a day's
# snapshot costs nothing until the hourly copy is pruned.
KEEP_HOURLY_DAYS="${KEEP_HOURLY_DAYS:-4}"
KEEP_DAILY_DAYS="${KEEP_DAILY_DAYS:-90}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# The audit log goes first, and the ordering is load-bearing rather than
# arbitrary. Everything below this block can `die` — sqlite3 missing, the
# journal gone — and a `die` before the audit snapshot would mean the record of
# every rejection quietly stopped being backed up because of a problem with a
# different file. This half needs only gzip, so it should not be able to fail
# for the other half's reasons.

# ---------------------------------------------------------------- audit log
#
# On the same timer as the journal, deliberately. The two records are only
# useful together — the journal says a trade happened, the audit log says what
# was proposed, what the gate ruled and what the model had been shown — so one
# timer, one unit to install and one log to read is the right shape. A second
# timer is a second thing that can be off without anyone noticing.

mkdir -p "$BACKUP_DIR/audit"

audit_copied=0
audit_skipped=0
audit_failed=0

# nullglob so a box with no audit files yet yields nothing rather than the
# literal pattern, which `set -e` would then turn into a failure on a fresh
# install. Scoped and restored, because it is a footgun anywhere else.
shopt -s nullglob
for src in "$AUDIT_DIR"/*.jsonl; do
  target="$BACKUP_DIR/audit/$(basename "$src").gz"

  # Skip anything already captured since the source last changed. A dated file
  # is finished the moment the UTC day rolls over and never changes again, so
  # after the first pass this re-compresses only today's — which is the one
  # still being appended to. Without it, every hour would re-gzip the entire
  # history to no purpose.
  if [[ -e "$target" && "$target" -nt "$src" ]]; then
    audit_skipped=$((audit_skipped + 1))
    continue
  fi

  work="$(mktemp "$BACKUP_DIR/audit/.$(basename "$src").XXXXXX")"
  # Guarded rather than relying on `set -e`: one unreadable file must not
  # abandon the rest of the history, and the failure still has to be visible.
  if ! gzip -9 -c "$src" > "$work" 2>/dev/null || ! gzip -t "$work" 2>/dev/null; then
    rm -f "$work"
    log "WARNING: could not snapshot $src"
    audit_failed=$((audit_failed + 1))
    continue
  fi
  mv "$work" "$target"
  chmod 640 "$target"
  audit_copied=$((audit_copied + 1))
done
shopt -u nullglob

# No pruning here, and that is a choice rather than an omission. The hourly
# journal snapshots are pruned because each is a full copy of the same
# database and the newest one supersedes the rest. These are not copies of each
# other: every dated file is a different day, and the day it covers is gone
# from nowhere else. A retention rule on the only record of a rejection is how
# that record quietly stops going back as far as anyone assumes. It is also
# cheap to keep — measured at roughly half a megabyte a day uncompressed, so
# well under a hundred megabytes a year once gzipped.
log "audit: $audit_copied snapshotted, $audit_skipped unchanged, $audit_failed failed \
($(find "$BACKUP_DIR/audit" -name '*.jsonl.gz' -type f | wc -l) on file)"

# Non-zero so `systemctl --failed` shows it. Deliberately last: the journal
# snapshot above has already been taken and kept, and losing that to an audit
# file with a bad permission bit would be the wrong way round.

command -v sqlite3 >/dev/null 2>&1 \
  || die "sqlite3 is not installed. Run deploy/bootstrap.sh, or: apt-get install -y sqlite3"

[[ -f "$DB" ]] || die "no database at $DB"

mkdir -p "$BACKUP_DIR/hourly" "$BACKUP_DIR/daily"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
today="$(date -u +%F)"
work="$(mktemp "$BACKUP_DIR/.journal-$stamp.XXXXXX")"

# Clean up the working copy on any exit path, including the integrity failure
# below. A half-written snapshot left in the backup directory is the one thing
# worse than no snapshot, because it looks like a snapshot.
trap 'rm -f "$work" "$work-wal" "$work-shm"' EXIT

log "snapshotting $DB"

# The busy timeout is load-bearing, not politeness. Without it `.backup` returns
# "database is locked" the instant the bot happens to be mid-write, and since
# the bot writes on every fill and every stand-down change, the snapshots that
# would fail are exactly the ones taken during the activity worth keeping. It
# fails closed, so the effect is not a corrupt backup but a missing one, which
# is only visible if somebody reads the journal. Found by running this script
# against a journal under a continuous writer.
#
# 30 seconds is far longer than any write this bot makes. If it is ever
# genuinely exhausted, something is holding a write transaction open and that is
# worth failing loudly about.
if ! error="$(sqlite3 -cmd '.timeout 30000' "$DB" ".backup '$work'" 2>&1)"; then
  die ".backup failed: ${error:-no output from sqlite3}"
fi

# A backup nobody has opened is a hope, not a backup. This is the whole reason
# the snapshot goes to a temporary name first.
check="$(sqlite3 "$work" 'PRAGMA integrity_check;' 2>&1 || true)"
[[ "$check" == "ok" ]] || die "integrity check failed on the snapshot: $check"

trades="$(sqlite3 "$work" 'SELECT COUNT(*) FROM trades;' 2>/dev/null || echo '?')"

gzip -9 "$work"
target="$BACKUP_DIR/hourly/journal-$stamp.db.gz"
mv "$work.gz" "$target"
chmod 640 "$target"
trap - EXIT

# One snapshot per day is promoted into daily/ by hard link, so pruning the
# hourly copy later does not take the day's copy with it.
daily="$BACKUP_DIR/daily/journal-$today.db.gz"
[[ -e "$daily" ]] || ln "$target" "$daily"

log "kept $target ($(stat -c%s "$target") bytes, $trades trades)"

# Prune by age. -mtime is whole days, which is why the hourly window is days
# rather than hours: an hourly retention would need find -mmin and would quietly
# round in the wrong direction.
find "$BACKUP_DIR/hourly" -name 'journal-*.db.gz' -type f \
  -mtime "+$KEEP_HOURLY_DAYS" -delete
find "$BACKUP_DIR/daily" -name 'journal-*.db.gz' -type f \
  -mtime "+$KEEP_DAILY_DAYS" -delete

log "retained $(find "$BACKUP_DIR/hourly" -name '*.db.gz' | wc -l) hourly, \
$(find "$BACKUP_DIR/daily" -name '*.db.gz' | wc -l) daily"

# Non-zero so `systemctl --failed` shows it, and last so it cannot pre-empt the
# journal snapshot. An audit file with a bad permission bit must not be the
# reason the journal went uncopied.
[[ "$audit_failed" -eq 0 ]] || die "$audit_failed audit file(s) could not be snapshotted"
