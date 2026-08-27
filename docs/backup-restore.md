# Backup, PITR and the restore drill

Section 11:

> **BACKUP:** Postgres PITR, hourly snapshot during Wari, restore drill
> documented and actually performed once.

Three deliverables, in the order they matter. The third is the one that is
usually skipped and the only one that proves the other two.

---

## Why two mechanisms

They fail differently, and they recover different things.

| | Hourly snapshot | PITR (WAL archiving) |
| --- | --- | --- |
| Recovers to | The last hour boundary | Any second |
| Worst-case data loss | 59 minutes | Seconds |
| Restore time | Minutes | Longer — base backup + WAL replay |
| Survives | Disk loss, corruption, a bad migration | The same, plus "someone ran the wrong UPDATE at 14:32" |

During the Wari, an hour of lost data is an hour of pass bookings, scans, and
possibly an incident timeline. That is not acceptable, which is why PITR is not
optional here — but PITR alone is slow to restore, so both run.

**The case PITR exists for:** an administrator runs a well-meant bulk update
against the wrong slot at 14:32 during a release window. A snapshot rolls back
to 14:00 and loses 32 minutes of real bookings. PITR rolls back to 14:31:59 and
loses nothing.

---

## 1. Hourly snapshot

`infra/backup/backup.sh`, run from cron:

```cron
# Every hour during the Wari. Off-season, daily is enough.
0 * * * * POSTGRES_PASSWORD=... /opt/wariverse/infra/backup/backup.sh >> /var/log/wariverse-backup.log 2>&1
```

What it does beyond `pg_dump`:

- **Refuses to start if the disk is tight.** A dump that fills the disk takes
  the database down with it — a half-written dump beside a wedged Postgres is
  the worst of both.
- **Verifies the archive it just wrote** and exits non-zero if it cannot be
  read. An unverified backup is a belief.
- **Writes a `.sha256` beside each dump**, checked by the drill.
- **Prunes past 72 hours.** This is the local tier, not the archive.

⚠️ **A backup on the same disk as the database is not a backup.** Point
`WARIVERSE_BACKUP_DIR` at separate storage, and replicate off-site. This
repository cannot enforce that.

Monitor the exit code. A cron job whose failures go to a log nobody reads is a
backup system that has already stopped working and not told anyone.

---

## 2. PITR — WAL archiving

In `postgresql.conf`:

```conf
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /var/backups/wariverse/wal/%f && cp %p /var/backups/wariverse/wal/%f'
archive_timeout = 300          # force a segment every 5 min even when quiet
max_wal_senders = 3
```

`archive_timeout = 300` matters more than it looks. Without it, a quiet period
leaves the last partial WAL segment unarchived — and the quiet period before a
release window is exactly when you would most regret the gap.

⚠️ **`cp` is a placeholder.** Use `pgBackRest` or `wal-g` for anything real:
they handle compression, retention, and the failure mode where the archive
destination is full — which `cp` handles by silently succeeding at nothing.

Take the base backup PITR replays onto **weekly**, and before the Wari:

```bash
pg_basebackup -h localhost -U wariverse -D /var/backups/wariverse/base -Ft -z -P
```

### Restoring to a point in time

```conf
# in the restored data directory's postgresql.conf
restore_command = 'cp /var/backups/wariverse/wal/%f %p'
recovery_target_time = '2026-07-25 14:31:59+05:30'
recovery_target_action = 'promote'
```

Then `touch recovery.signal` and start Postgres. **Restore to a scratch
instance first and check it before pointing the application at it** — a
recovery target chosen from memory under pressure is frequently the wrong one,
and promoting is not reversible.

---

## 2b. TimescaleDB: why generic Postgres backup advice is wrong here

**Read this before restoring anything by hand.**

`density_readings`, `forecasts` and `dindi_pings` are hypertables. What looks
like one table is a parent plus a chunk per time interval, with catalog rows
tying them together. A plain `pg_restore` re-inserts those catalog rows *and*
replays the chunk DDL, and the two fight.

The bad outcome is not an error — it is a restore that **succeeds** and leaves
you with ordinary tables where hypertables used to be. Every row is present.
Every count matches. The time partitioning is gone, so the first wide time-range
query in the command centre scans the whole of the highest-volume table in the
system instead of one chunk, and you find out under load, months later.

So any manual restore must bracket `pg_restore` with:

```sql
SELECT timescaledb_pre_restore();   -- before
-- ... pg_restore ...
SELECT timescaledb_post_restore();  -- after
```

`restore-drill.sh` does this, and then **verifies it worked** by counting
`timescaledb_information.hypertables` — because the whole point is that the
failure is silent.

Setting the flag from a *separate* `psql` connection works because
`timescaledb_pre_restore()` sets a database-level GUC, not a session-local one
— this is TimescaleDB's documented sequence, not a trick.

One consequence: the drill restores with `--single-transaction` rather than
`--jobs`, because `pg_restore` rejects that combination and for this database
atomicity is worth more than parallelism. Either the whole schema and every
hypertable chunk lands, or nothing does; a half-restored TimescaleDB catalog is
hard to reason about and easy to mistake for a working database. If restore time
ever becomes the binding constraint, buy it back by splitting the dump rather
than by giving up atomicity.

## 3. The restore drill

`infra/backup/restore-drill.sh` — repeatable so it gets repeated:

```bash
POSTGRES_PASSWORD=... ./infra/backup/restore-drill.sh
```

It restores the newest snapshot into a throwaway database, checks the result,
and drops it. It never touches the live database.

**What it checks, and why each one:**

| Check | Why |
| --- | --- |
| SHA-256 matches | The file has not changed since it was written |
| `pg_restore --exit-on-error` | A partial restore is a failure, not a surprise found later |
| `alembic_version` present | The schema came back at a known migration |
| users / zones non-empty | It is not a backup of an empty database |
| `trg_audit_log_no_update` restored | A restored database whose audit log is editable is not the one that was backed up |
| `trg_breach_evidence_immutable` restored | Same, for evidence |
| Hypertables restored as hypertables | Catches the silent TimescaleDB failure in §2b |
| Breach chain links intact | **The interesting one** — see below |
| Breach sequence has no gaps | A missing record shows up independently of a broken link |

**Why the chain verification is the point.** A dump that restores cleanly but
whose breach ledger no longer verifies would mean the backup path itself
corrupted evidence. The ledger's entire purpose is to survive the pressure under
which someone would want it not to; a backup/restore cycle that silently broke
it would defeat that without anyone noticing. The drill recomputes the links in
SQL, so it needs nothing running but Postgres.

**What the drill does NOT prove:** that a production restore will be fast
enough. That depends on hardware and data volume — measure it separately, on
the real machine, and write the number down.

### Cadence

- Before every Wari — **mandatory**
- Monthly otherwise
- After any Postgres major-version upgrade
- After any change to `backup.sh`

### Drill log

Record every run. A drill nobody recorded is a drill nobody can prove happened,
and "when did we last verify we could restore" is the first question after an
incident.

| Date | Snapshot | Restore time | Verdict | Run by | Notes |
| --- | --- | --- | --- | --- | --- |
| _(pending)_ | | | | | **Not yet performed — see below** |

> ⚠️ **Section 11 requires this drill to be "actually performed once", and it
> has not been.** The scripts are written, syntax-checked and their assertions
> reviewed against the real schema (both trigger names verified against
> migration `0003`), but no drill has been executed — the development machine
> ran out of disk during Phase 10, which stopped Postgres. **This is the one
> Phase 10 deliverable that is not done, and it is not done until a row appears
> in the table above.**
>
> To close it: free disk, `docker compose up -d db`, run `backup.sh`, then
> `restore-drill.sh`, and record the result.

---

## 4. What is deliberately not backed up

- **Redis.** It holds cache, WebSocket fan-out and job locks. Everything in it
  is derived and TTL'd; restoring a stale snapshot of it would be actively
  worse than starting empty, because expired zone snapshots would come back and
  briefly render as live.
- **Prometheus and Grafana volumes.** Operational telemetry. The dashboards are
  provisioned from the repository, so a lost Grafana comes back identical.
- **Breach clip blobs**, unless your object store is included in the same
  policy. ⚠️ Check this — the ledger rows are in Postgres, but the clips they
  reference may not be, and a chain that verifies against missing clips is only
  half the evidence.

---

## 5. Recovery objectives

| | Target | Mechanism |
| --- | --- | --- |
| RPO (data loss) | < 5 min | WAL archiving with `archive_timeout=300` |
| RTO (time to restore) | < 30 min | Hourly custom-format dump, parallel `pg_restore` |
| Availability | 99.9% during the Wari window | Section 11 |

⚠️ Both targets are **stated, not measured.** Measure them during the drill on
the real hardware and replace these numbers with observed ones. A target nobody
has timed is a hope.
