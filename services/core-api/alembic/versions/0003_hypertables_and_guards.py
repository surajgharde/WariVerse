"""Hypertables, 1-minute rollups, append-only audit guard, seed config.

Revision ID: 0003_hypertables_and_guards
Revises: 0002_initial_schema
Create Date: 2026-08-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0003_hypertables_and_guards"
down_revision: str | None = "0002_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Imported rather than duplicated so the seed and the code cannot drift.
from app.models.audit import DEFAULT_CONFIG  # noqa: E402


def upgrade() -> None:
    conn = op.get_bind()

    # --- TimescaleDB hypertables -------------------------------------------
    # 40 cameras x 6 writes/minute per zone; time partitioning from day one so
    # the Wari week does not land in a single 200M-row heap.
    conn.execute(
        text(
            "SELECT create_hypertable('density_readings', 'time', "
            "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE, migrate_data => TRUE)"
        )
    )
    conn.execute(
        text(
            "SELECT create_hypertable('dindi_pings', 'time', "
            "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE, migrate_data => TRUE)"
        )
    )

    # --- 1-minute rollups (Section 4/M2 step 6) ----------------------------
    # Continuous aggregates cannot be created inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS density_readings_1min
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket(INTERVAL '1 minute', time) AS bucket,
                zone_id,
                AVG(density)            AS avg_density,
                MAX(density)            AS peak_density,
                AVG(person_count)       AS avg_person_count,
                MAX(stagnation_index)   AS peak_stagnation,
                MAX(counterflow_ratio)  AS peak_counterflow,
                AVG(confidence)         AS avg_confidence,
                COUNT(*)                AS sample_count
            FROM density_readings
            GROUP BY bucket, zone_id
            WITH NO DATA
            """
        )
        op.execute(
            """
            SELECT add_continuous_aggregate_policy('density_readings_1min',
                start_offset => INTERVAL '2 hours',
                end_offset   => INTERVAL '1 minute',
                schedule_interval => INTERVAL '1 minute',
                if_not_exists => TRUE)
            """
        )

    # --- append-only audit log (Section 2, Section 12) ---------------------
    # A grant can be changed by whoever holds the role.  A trigger has to be
    # dropped, and dropping it is a schema change someone can see.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wariverse_block_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only: % on audit_log is not permitted', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log")
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_no_update
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION wariverse_block_audit_mutation();
        """
    )

    # The breach ledger is likewise write-once for its evidence fields.  Review
    # columns are still editable — a human review is the point — but the hash
    # chain, the clip and the time it happened are not.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION wariverse_protect_breach_evidence()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.chain_hash    IS DISTINCT FROM OLD.chain_hash
            OR NEW.prev_hash     IS DISTINCT FROM OLD.prev_hash
            OR NEW.clip_sha256   IS DISTINCT FROM OLD.clip_sha256
            OR NEW.occurred_at   IS DISTINCT FROM OLD.occurred_at
            OR NEW.sequence      IS DISTINCT FROM OLD.sequence
            OR NEW.payload_snapshot IS DISTINCT FROM OLD.payload_snapshot THEN
                RAISE EXCEPTION
                    'breach_events evidence fields are immutable'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_breach_evidence_immutable ON breach_events")
    op.execute(
        """
        CREATE TRIGGER trg_breach_evidence_immutable
        BEFORE UPDATE ON breach_events
        FOR EACH ROW EXECUTE FUNCTION wariverse_protect_breach_evidence();
        """
    )

    # --- seed operator-tunable configuration -------------------------------
    for key, (value, description) in DEFAULT_CONFIG.items():
        conn.execute(
            text(
                "INSERT INTO system_config (key, value, description, created_at, updated_at) "
                "VALUES (:key, CAST(:value AS jsonb), :description, now(), now()) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps({"v": value}), "description": description},
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_breach_evidence_immutable ON breach_events")
    op.execute("DROP FUNCTION IF EXISTS wariverse_protect_breach_evidence()")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS wariverse_block_audit_mutation()")
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS density_readings_1min")
    op.execute("DELETE FROM system_config")
    # Hypertables are left in place: reversing them would rewrite every chunk
    # for no benefit, and dropping the tables belongs to 0002's downgrade.
