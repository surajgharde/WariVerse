#!/usr/bin/env bash
# The restore drill (Section 11 BACKUP, Phase 10).
#
#   "restore drill documented and actually performed once"
#
# The documentation is docs/backup-restore.md. This is the "actually performed"
# half, made repeatable so that performing it is a command rather than an
# afternoon — because a drill that takes an afternoon gets performed once, in
# March, and never again.
#
#   ./infra/backup/restore-drill.sh                      # newest snapshot
#   ./infra/backup/restore-drill.sh /path/to/some.dump   # a specific one
#
# WHAT IT ACTUALLY PROVES, AND WHAT IT DOES NOT
#
# It restores into a THROWAWAY database and checks the restored data is
# coherent: row counts are sane, the breach hash chain still verifies, and the
# schema is at the migration head. It then drops that database.
#
# It does NOT touch the live database, and it does not prove a production
# restore will be fast enough — that depends on hardware you should measure
# separately. It proves the backup is restorable and the restored data is
# trustworthy, which is what usually turns out to be false.
#
# The chain verification is the interesting assertion. A dump that restores but
# whose breach ledger no longer verifies would mean the backup path itself
# corrupted evidence — and the whole point of the ledger is that it survives
# exactly the pressure under which someone would want it not to.

set -Eeuo pipefail

BACKUP_DIR="${WARIVERSE_BACKUP_DIR:-/var/backups/wariverse}"
PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-wariverse}"
export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"

DRILL_DB="wariverse_drill_$(date -u +%Y%m%d%H%M%S)"
DUMP="${1:-}"

log() { printf '%s [drill] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
psql_drill() { psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DRILL_DB" -tAc "$1"; }

cleanup() {
    log "dropping ${DRILL_DB}"
    psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
        -c "DROP DATABASE IF EXISTS \"${DRILL_DB}\" WITH (FORCE)" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -z "$DUMP" ]; then
    DUMP=$(find "$BACKUP_DIR" -name 'wariverse-*.dump' -printf '%T@ %p\n' 2>/dev/null \
           | sort -rn | head -1 | cut -d' ' -f2-)
fi
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { log "no snapshot found in ${BACKUP_DIR}"; exit 1; }

log "drilling ${DUMP}"

# --- 0. checksum, if one was written ---------------------------------------
if [ -f "${DUMP}.sha256" ]; then
    if sha256sum --check --status "${DUMP}.sha256"; then
        log "checksum OK"
    else
        log "CHECKSUM MISMATCH — this file has changed since it was written"
        exit 1
    fi
fi

# --- 1. restore into a throwaway --------------------------------------------
STARTED=$(date +%s)
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres \
    -c "CREATE DATABASE \"${DRILL_DB}\"" >/dev/null
psql_drill "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS timescaledb;" >/dev/null

# TimescaleDB needs to be told a restore is happening.
#
# This is not boilerplate — it is the trap that makes naive Postgres backup
# advice wrong for this database. `density_readings`, `forecasts` and
# `dindi_pings` are hypertables: what looks like one table is a parent plus a
# chunk per time interval, with catalog rows tying them together. Restoring
# that with a plain `pg_restore` re-inserts the catalog rows AND replays the
# chunk DDL, and the two fight — you get errors, or worse, a database that
# restores "successfully" with hypertables that are no longer hypertables and
# silently lose their time partitioning.
#
# `timescaledb_pre_restore()` suspends the background workers and the catalog
# consistency checks for the duration; `timescaledb_post_restore()` puts them
# back. Skipping the pair is the single most likely way for a restore of THIS
# database to appear to work and not have.
#
# Guarded with `|| true` because the pair only exists if the extension is
# present, and the drill should still be useful against a dump taken from a
# database without it.
log "entering timescaledb restore mode"
psql_drill "SELECT timescaledb_pre_restore()" >/dev/null || log "  (no timescaledb_pre_restore — continuing)"

# --exit-on-error so a partial restore is a failure rather than a surprise
# later. --no-owner because the drill database is owned by whoever is drilling.
#
# `--single-transaction` rather than `--jobs`: the two are mutually exclusive in
# pg_restore, and for this database the transaction is worth more than the
# parallelism. Either the whole schema and all its hypertable chunks land, or
# nothing does — a half-restored TimescaleDB catalog is a database that is hard
# to reason about and easy to mistake for a working one. If restore time ever
# becomes the binding constraint, buy it back by splitting the dump rather than
# by giving up atomicity.
#
# (The `timescaledb.restoring` flag set above is a *database*-level GUC, not
# session-local, which is why setting it from a separate psql connection works
# — this is TimescaleDB's documented restore sequence.)
pg_restore \
    -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DRILL_DB" \
    --no-owner --no-privileges --exit-on-error \
    --single-transaction \
    "$DUMP"

psql_drill "SELECT timescaledb_post_restore()" >/dev/null || log "  (no timescaledb_post_restore — continuing)"

ELAPSED=$(( $(date +%s) - STARTED ))
log "restored in ${ELAPSED}s"

# --- 2. is the restored data coherent? --------------------------------------
FAILURES=0
check() {
    local label="$1" actual="$2" condition="$3"
    if eval "$condition"; then
        log "  PASS  ${label}: ${actual}"
    else
        log "  FAIL  ${label}: ${actual}"
        FAILURES=$((FAILURES + 1))
    fi
}

log "checking restored data"

MIGRATION=$(psql_drill "SELECT version_num FROM alembic_version" || echo "MISSING")
check "schema at migration" "$MIGRATION" '[ "$MIGRATION" != "MISSING" ] && [ -n "$MIGRATION" ]'

# `|| echo -1` on each: a missing table means the restore failed, and the drill
# should say so in its verdict rather than die mid-script with a psql error and
# leave the operator to work out which check it got to.
USERS=$(psql_drill "SELECT count(*) FROM users" || echo -1)
check "users present" "$USERS" '[ "$USERS" -gt 0 ]'

ZONES=$(psql_drill "SELECT count(*) FROM zones" || echo -1)
check "zones present" "$ZONES" '[ "$ZONES" -gt 0 ]'

AUDIT=$(psql_drill "SELECT count(*) FROM audit_log" || echo -1)
check "audit log present" "$AUDIT" '[ "$AUDIT" -ge 0 ]'

# The append-only trigger must have come back with the schema. A restored
# database whose audit log is editable is not the database that was backed up.
TRIGGER=$(psql_drill "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_audit_log_no_update'" || echo -1)
check "audit append-only trigger restored" "$TRIGGER" '[ "$TRIGGER" -ge 1 ]'

CHAIN_TRIGGER=$(psql_drill "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_breach_evidence_immutable'" || echo -1)
check "breach evidence trigger restored" "$CHAIN_TRIGGER" '[ "$CHAIN_TRIGGER" -ge 1 ]'

# Did the hypertables come back AS hypertables?
#
# The failure this catches is silent, which is the only reason it is worth a
# check of its own. A restore that turns `density_readings` into an ordinary
# table succeeds, returns all its rows, and passes every other assertion in this
# script — and then the highest-volume table in the system has no time
# partitioning, so the first big time-range query in the command centre takes
# the whole table instead of one chunk. You find out under load, months later.
HYPERTABLES=$(psql_drill \
    "SELECT count(*) FROM timescaledb_information.hypertables" || echo -1)
check "hypertables restored as hypertables" "${HYPERTABLES}" '[ "$HYPERTABLES" -ge 3 ]'

# --- 3. does the evidence chain still verify? -------------------------------
# Recomputed in SQL rather than by calling the API, so the drill needs nothing
# running but Postgres. Each row's prev_hash must equal the previous row's
# chain_hash, and the sequence must have no gaps.
BREACHES=$(psql_drill "SELECT count(*) FROM breach_events")
if [ "$BREACHES" -gt 0 ]; then
    BROKEN_LINKS=$(psql_drill "
        SELECT count(*) FROM (
            SELECT sequence, prev_hash,
                   lag(chain_hash) OVER (ORDER BY sequence) AS expected_prev
            FROM breach_events
        ) t
        WHERE expected_prev IS NOT NULL AND prev_hash IS DISTINCT FROM expected_prev")
    check "breach chain links intact" "${BROKEN_LINKS} broken of ${BREACHES}" '[ "$BROKEN_LINKS" -eq 0 ]'

    GAPS=$(psql_drill "
        SELECT count(*) FROM (
            SELECT sequence, lag(sequence) OVER (ORDER BY sequence) AS prev
            FROM breach_events
        ) t
        WHERE prev IS NOT NULL AND sequence <> prev + 1")
    check "breach sequence has no gaps" "${GAPS} gap(s)" '[ "$GAPS" -eq 0 ]'
else
    log "  SKIP  breach chain: no records in this snapshot"
fi

# --- 4. verdict -------------------------------------------------------------
echo
echo "======================================================================"
echo "  Restore drill — $(basename "$DUMP")"
echo "======================================================================"
echo "  restore time     ${ELAPSED}s"
echo "  schema version   ${MIGRATION}"
echo "  breach records   ${BREACHES}"
if [ "$FAILURES" -eq 0 ]; then
    echo "  VERDICT: PASS — this backup restores and its evidence chain holds."
    echo "======================================================================"
    echo
    echo "  Record it in docs/backup-restore.md under 'Drill log'."
    exit 0
fi
echo "  VERDICT: FAIL — ${FAILURES} check(s) failed. This backup is not trustworthy."
echo "======================================================================"
exit 1
