"""Indexes and configuration the crowd engine needs (Phase 3).

Revision ID: 0005_crowd_engine
Revises: 0004_pass_early_reslot
Create Date: 2026-08-24

No new tables: the Phase 1 schema already carries `zones`, `cameras`,
`density_readings` and `alerts`, which is what "the schema covers every table
the later phases need" was for.  What Phase 3 adds is the access patterns.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0005_crowd_engine"
down_revision: str | None = "0004_pass_early_reslot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: New tunables.  Added here rather than to DEFAULT_CONFIG's migration so an
#: existing database picks them up without re-running 0003.
NEW_CONFIG: dict[str, tuple[object, str]] = {
    "alert_cooldown_seconds": (
        180,
        "How long an open alert absorbs repeat triggers before it re-notifies.",
    ),
    "camera_offline_seconds": (
        120,
        "Silence from a camera for this long marks it offline and drops its zone's confidence.",
    ),
    "crowd_window_seconds": (
        10,
        "Aggregation window the AI engine reports on. Section 4/M2 step 4.",
    ),
    "crowd_sample_fps": (
        2.0,
        "Frames per second sampled from each stream. 30 FPS is wasted compute for density.",
    ),
    "sim_baseline_multiplier": (
        1.0,
        "Scales the simulation engine's diurnal baseline. 1.0 is an ordinary Wari day.",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()

    # The alert reconciler asks "is there a live alert of this type for this
    # zone" on every ingest — forty zones every ten seconds.  Partial, because
    # resolved and expired rows are never the answer and there will eventually
    # be far more of them than live ones.
    op.create_index(
        "ix_alerts_zone_type_live",
        "alerts",
        ["zone_id", "type"],
        postgresql_where=sa.text("status IN ('open', 'acknowledged', 'escalated')"),
    )

    # The feed's default query: live alerts from the last day, worst first.
    op.create_index(
        "ix_alerts_live_created",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("status IN ('open', 'acknowledged', 'escalated')"),
    )

    # The camera watchdog scans for stale heartbeats every minute.
    op.create_index(
        "ix_cameras_status_heartbeat",
        "cameras",
        ["status", "last_heartbeat_at"],
    )

    # `crowd_service.latest` falls back to DISTINCT ON (zone_id) ... ORDER BY
    # time DESC when the Redis snapshot has expired.  Descending time makes that
    # a backwards index scan instead of a sort over the day's chunk.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_density_readings_zone_time_desc "
        "ON density_readings (zone_id, time DESC)"
    )

    for key, (value, description) in NEW_CONFIG.items():
        conn.execute(
            text(
                "INSERT INTO system_config (key, value, description, created_at, updated_at) "
                "VALUES (:key, CAST(:value AS jsonb), :description, now(), now()) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps({"v": value}), "description": description},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for key in NEW_CONFIG:
        conn.execute(text("DELETE FROM system_config WHERE key = :key"), {"key": key})

    op.execute("DROP INDEX IF EXISTS ix_density_readings_zone_time_desc")
    op.drop_index("ix_cameras_status_heartbeat", table_name="cameras")
    op.drop_index("ix_alerts_live_created", table_name="alerts")
    op.drop_index("ix_alerts_zone_type_live", table_name="alerts")
