#!/usr/bin/env bash
# Hourly Postgres snapshot (Section 11 BACKUP, Phase 10).
#
#   BACKUP: Postgres PITR, hourly snapshot during Wari, restore drill
#           documented and actually performed once.
#
# This is the snapshot half. The PITR half is WAL archiving, configured in
# postgresql.conf — see docs/backup-restore.md, which also carries the drill.
#
# Run hourly during the Wari:
#   0 * * * * /opt/wariverse/infra/backup/backup.sh >> /var/log/wariverse-backup.log 2>&1
#
# TWO DECISIONS WORTH KNOWING ABOUT
#
# 1. `--format=custom`, not plain SQL. It compresses, and more importantly it
#    lets `pg_restore` do a selective or parallel restore. At 3 a.m. with the
#    Wari peaking in six hours, the difference between a 40-minute restore and a
#    10-minute one is the whole point of taking backups.
#
# 2. The script VERIFIES the dump it just took and fails loudly if it cannot.
#    An unverified backup is a belief, not a backup, and the moment you find out
#    is the moment you needed it.

set -Eeuo pipefail

BACKUP_DIR="${WARIVERSE_BACKUP_DIR:-/var/backups/wariverse}"
RETENTION_HOURS="${WARIVERSE_BACKUP_RETENTION_HOURS:-72}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-wariverse}"
PGDATABASE="${POSTGRES_DB:-wariverse}"
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/wariverse-${STAMP}.dump"

log() { printf '%s [backup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

fail() {
    log "FAILED: $*"
    # Non-zero exit is what a monitored cron turns into an alert. A backup
    # script that swallows its own failure is worse than no backup script:
    # it manufactures confidence.
    exit 1
}
trap 'fail "aborted at line $LINENO"' ERR

mkdir -p "$BACKUP_DIR"

# --- free space check, before rather than after -----------------------------
# A dump that fills the disk takes the database down with it. Refusing to start
# is strictly better than a half-written dump beside a wedged Postgres.
AVAILABLE_MB=$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')
DB_SIZE_MB=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -tAc \
    "SELECT ceil(pg_database_size('${PGDATABASE}')/1024.0/1024.0)")
NEEDED_MB=$(( DB_SIZE_MB / 2 + 512 ))   # custom format compresses ~2x, plus headroom

if [ "$AVAILABLE_MB" -lt "$NEEDED_MB" ]; then
    fail "only ${AVAILABLE_MB}MB free in ${BACKUP_DIR}, need ~${NEEDED_MB}MB. Not starting."
fi

log "database ${DB_SIZE_MB}MB, ${AVAILABLE_MB}MB free, dumping to ${TARGET}"

# --- the dump ---------------------------------------------------------------
pg_dump \
    -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
    --format=custom \
    --compress=6 \
    --file="$TARGET"

# --- verify it ---------------------------------------------------------------
# `pg_restore --list` parses the archive's table of contents. It does not prove
# every byte restores, but it proves the file is a readable archive rather than
# a truncated one — which is the failure that actually happens when a disk fills
# or a network share drops mid-write.
TOC_LINES=$(pg_restore --list "$TARGET" | grep -c '^[0-9]' || true)
if [ "${TOC_LINES:-0}" -lt 20 ]; then
    fail "dump verified as unreadable or near-empty (${TOC_LINES} TOC entries)"
fi

# A sanity check on content, not just structure. A backup of an empty database
# is a readable archive too.
if ! pg_restore --list "$TARGET" | grep -q 'TABLE DATA public breach_events'; then
    log "WARNING: breach_events not present in the dump — check this is the right database"
fi

SIZE_MB=$(( $(stat -c %s "$TARGET") / 1024 / 1024 ))
sha256sum "$TARGET" > "${TARGET}.sha256"
log "wrote ${SIZE_MB}MB, ${TOC_LINES} TOC entries, checksum beside it"

# --- prune ------------------------------------------------------------------
# Hourly snapshots for three days. Anything older is covered by whatever
# off-site policy the deployment has; this script is the local tier and is not
# an archive.
PRUNED=$(find "$BACKUP_DIR" -name 'wariverse-*.dump' -mmin "+$((RETENTION_HOURS * 60))" -print -delete | wc -l)
find "$BACKUP_DIR" -name 'wariverse-*.dump.sha256' -mmin "+$((RETENTION_HOURS * 60))" -delete
# An `if` rather than `[ ... ] && log ...`: under `set -e` a trailing `&&` list
# whose test is false exits non-zero, so the short form would make every run
# with nothing to prune report itself as a FAILED backup. That is the worst
# possible false alarm — it trains whoever reads the log to ignore it.
if [ "$PRUNED" -gt 0 ]; then
    log "pruned ${PRUNED} snapshot(s) older than ${RETENTION_HOURS}h"
fi

log "OK ${TARGET}"
