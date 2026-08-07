#!/usr/bin/env bash
#
# Talent Pilot backup.
#
# Snapshots everything under DATA_DIR — the accounts database, every user's
# jobs database, resume profiles, saved answers, and Gmail tokens — into a
# single dated tarball, then prunes anything older than KEEP_DAYS.
#
# Installed by deploy/setup.sh to run nightly. Run by hand any time:
#   sudo /usr/local/bin/talent-pilot-backup

set -euo pipefail

DATA_DIR="${DATA_DIR:-/var/lib/talent-pilot}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/talent-pilot}"
KEEP_DAYS="${KEEP_DAYS:-14}"

STAMP="$(date +%F-%H%M)"
ARCHIVE="${BACKUP_DIR}/talent-pilot-${STAMP}.tar.gz"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') | $*"; }

[[ -d "$DATA_DIR" ]] || { log "ERROR: ${DATA_DIR} does not exist"; exit 1; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "Backing up ${DATA_DIR}"

# SQLite databases go through the online backup API rather than a file copy.
# Copying a live database risks capturing it mid-write — and with WAL enabled
# a plain cp can miss committed data still sitting in the -wal file. This
# produces a consistent snapshot without stopping the services.
db_count=0
while IFS= read -r -d '' db; do
    rel="${db#"$DATA_DIR"/}"
    mkdir -p "${WORK}/$(dirname "$rel")"
    sqlite3 "$db" ".backup '${WORK}/${rel}'"
    db_count=$((db_count + 1))
done < <(find "$DATA_DIR" -name '*.db' -type f -print0)

# Everything that is not a database: profiles, answers, Gmail tokens, sync
# timestamps. The -wal and -shm files are deliberately skipped — they belong
# to the live databases and are already folded into the snapshots above.
rsync -a \
    --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
    "${DATA_DIR}/" "${WORK}/"

tar czf "$ARCHIVE" -C "$WORK" .
chmod 600 "$ARCHIVE"

size="$(du -h "$ARCHIVE" | cut -f1)"
log "Wrote ${ARCHIVE} (${size}, ${db_count} databases)"

# Prune old archives.
removed="$(find "$BACKUP_DIR" -name 'talent-pilot-*.tar.gz' -mtime "+${KEEP_DAYS}" -print -delete | wc -l)"
[[ "$removed" -gt 0 ]] && log "Pruned ${removed} archive(s) older than ${KEEP_DAYS} days"

kept="$(find "$BACKUP_DIR" -name 'talent-pilot-*.tar.gz' | wc -l)"
log "Done. ${kept} archive(s) retained."
