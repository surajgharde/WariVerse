"""The forecast table and its tunables (Phase 8, Section 4/M6).

Revision ID: 0006_forecasts
Revises: 0005_crowd_engine
Create Date: 2026-08-26

This is the first phase that needs a table Phase 1 did not anticipate.  The
Phase 1 schema covers the *entities* the product describes — zones, passes,
incidents, breaches — and a forecast is not an entity, it is a claim about one.
Storing it rather than recomputing it on read is what makes a prediction
answerable after the fact: `target_at` is a real column so a scoring query can
join the forecast to the density reading that eventually arrived for that
minute, and say whether the model was right.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0006_forecasts"
down_revision: str | None = "0005_crowd_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CONFIG: dict[str, tuple[object, str]] = {
    "forecast_interval_seconds": (
        300,
        "How often the engine publishes a new set of forecasts. Section 4/M6 horizons are 30/60/90 min.",
    ),
    "forecast_stale_seconds": (
        900,
        "A forecast older than this is reported as unavailable rather than shown. Three publish cycles.",
    ),
    "forecast_retention_days": (
        30,
        "How long forecasts are kept for scoring against what actually happened.",
    ),
    "forecast_alert_horizon_minutes": (
        60,
        "Which horizon may raise a forecast_high alert. Longer horizons are shown but never paged on.",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        "forecasts",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("horizon_minutes", sa.Integer(), nullable=False),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("predicted_density", sa.Float(), nullable=False),
        sa.Column("predicted_level", sa.String(length=12), nullable=False),
        sa.Column("interval_low", sa.Float(), nullable=False),
        sa.Column("interval_high", sa.Float(), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("trained_on", sa.String(length=32), nullable=False),
        sa.Column("validation_mae", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("issued_at", "zone_id", "horizon_minutes"),
        # An interval whose bounds are the wrong way round is a bug that would
        # otherwise render as a band drawn backwards, which reads as a narrow
        # band rather than as an error.
        sa.CheckConstraint("interval_low <= predicted_density", name="ck_forecast_interval_low"),
        sa.CheckConstraint("predicted_density <= interval_high", name="ck_forecast_interval_high"),
        sa.CheckConstraint("horizon_minutes > 0", name="ck_forecast_horizon_positive"),
        # Density is people per square metre. Negative is meaningless and the
        # physical ceiling is around 7; 20 leaves room for a bad model to be
        # visibly bad without letting a parsing error through as 1e9.
        sa.CheckConstraint("predicted_density >= 0 AND predicted_density <= 20", name="ck_forecast_density_range"),
    )

    op.create_index("ix_forecasts_zone_target", "forecasts", ["zone_id", "target_at"])
    op.create_index("ix_forecasts_issued_desc", "forecasts", ["issued_at"])
    op.create_index("ix_forecasts_target_at", "forecasts", ["target_at"])

    # Lower volume than density_readings by two orders of magnitude (40 zones x
    # 3 horizons every 5 minutes, not 6 writes a minute per camera), so a weekly
    # chunk rather than a daily one. Still a hypertable: the scoring query that
    # makes forecasts worth keeping is a time-range scan, and that is what the
    # partitioning is for.
    conn.execute(
        text(
            "SELECT create_hypertable('forecasts', 'issued_at', "
            "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE, migrate_data => TRUE)"
        )
    )

    # `GET /forecast` asks for the newest issue per zone on every console poll.
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_forecasts_zone_issued_desc "
            "ON forecasts (zone_id, horizon_minutes, issued_at DESC)"
        )
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

    op.execute("DROP INDEX IF EXISTS ix_forecasts_zone_issued_desc")
    op.drop_index("ix_forecasts_target_at", table_name="forecasts")
    op.drop_index("ix_forecasts_issued_desc", table_name="forecasts")
    op.drop_index("ix_forecasts_zone_target", table_name="forecasts")
    op.drop_table("forecasts")
