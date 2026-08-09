#!/usr/bin/env bash
#
# Snapshot data/journal.db. Run hourly by mudhorn-backup.timer.
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
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"

# Hourly snapshots are kept for a few days; the first snapshot of each day is
# also linked into daily/ and kept for a quarter. Hard links, so a day's
# snapshot costs nothing until the hourly copy is pruned.
KEEP_HOURLY_DAYS="${KEEP_HOURLY_DAYS:-4}"
KEEP_DAILY_DAYS="${KEEP_DAILY_DAYS:-90}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

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
