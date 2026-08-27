"""Palkhi schedule, Dindi tracking state, and the assistant transcript.

Revision ID: 0007_palkhi_and_assistant
Revises: 0006_forecasts
Create Date: 2026-08-26

Phase 1 built the *entities* Section 8 names — `dindis`, `dindi_pings`,
`halt_towns` have existed since the initial schema.  What Phase 9 adds is the
part Section 4/M8 asks for that is not an entity:

* **the planned halt schedule** (`dindi_schedule`).  Section 8 put
  `expected_arrival` on `halt_towns`, which works for a route with one Palkhi
  and breaks the moment forty Dindis share the Alandi road and reach Saswad on
  different evenings.  A town's real question is "who is arriving at me
  tonight, and how many", and that is a join table, not a column.
* **derived tracking state** on `dindis` — status, last ping, position along
  the route.  Denormalised from the ping hypertable because the readiness board
  reads every active Dindi on every refresh, and the alternative is one
  time-series scan per Dindi per poll.
* **`assistant_turns`** — Section 13's "log every assistant turn with its tool
  calls for review".  The assistant is the only component here that emits
  sentences nobody wrote; its contract is that every factual claim came from a
  tool call, and that contract is unfalsifiable unless the calls are stored
  beside the answer.

`dindi_pings` gains `route_fraction` and `off_route_m` rather than computing
them on read: the pace estimate is a difference between two fractions, and
re-projecting the ping history onto the route line on every deviation sweep is
the same work done hourly instead of once.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_palkhi_and_assistant"
down_revision: str | None = "0006_forecasts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CONFIG: dict[str, tuple[object, str]] = {
    # --- Palkhi tracking (Section 4/M8) ---------------------------------
    "dindi_deviation_minutes": (
        45,
        "Schedule deviation that notifies the next halt town. Section 4/M8 names 45 minutes.",
    ),
    "dindi_ping_interval_seconds": (
        60,
        "How often a Dindi's designated device should report. Section 4/M8: 60s, battery-aware.",
    ),
    "dindi_signal_lost_minutes": (
        20,
        "No ping for this long and the Dindi reads as signal_lost, not as walking. Twenty missed pings.",
    ),
    "dindi_pace_window_minutes": (
        90,
        "Window the walking pace is averaged over. Long enough to survive a tea stop.",
    ),
    "dindi_off_route_alert_m": (
        500,
        "Distance from the route line that means the group has left the route, not stepped aside.",
    ),
    "dindi_halt_arrival_radius_m": (
        800,
        "Distance from a halt town centre that counts as having arrived, when the town has no polygon.",
    ),
    # Provisioning ratios for the halt-town readiness board. In system_config
    # rather than in code, unlike the density bands: these are planning
    # conventions a district administration will argue about and adjust, not
    # published crowd-safety limits. The board states the ratio it used, so a
    # changed number is visible in the output rather than hidden behind it.
    "halt_water_points_per_1000": (
        4.0,
        "Water points per 1000 expected pilgrims (1 per 250). Used to grade halt-town readiness.",
    ),
    "halt_sanitation_units_per_1000": (
        10.0,
        "Sanitation units per 1000 expected pilgrims (1 per 100) for an overnight halt.",
    ),
    "halt_medical_camps_per_10000": (
        1.0,
        "Medical camps per 10000 expected pilgrims at a halt town.",
    ),
    # --- Assistant (Section 13) -----------------------------------------
    "assistant_enabled": (
        True,
        "Master switch for the pilgrim assistant. Off falls back to the deterministic answers.",
    ),
    "assistant_max_turns_per_hour": (
        30,
        "Per-session turn ceiling. Stops a loop from spending the API budget, not a person from asking.",
    ),
    "assistant_turn_retention_days": (
        90,
        "How long assistant transcripts are kept for review before purge (Section 12).",
    ),
}


def upgrade() -> None:
    conn = op.get_bind()

    # ---------------------------------------------------------------- dindis
    op.add_column(
        "dindis",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="registered"),
    )
    op.add_column("dindis", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dindis", sa.Column("last_ping_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("dindis", sa.Column("last_battery", sa.Integer(), nullable=True))
    op.add_column("dindis", sa.Column("route_fraction", sa.Float(), nullable=True))
    op.add_column("dindis", sa.Column("current_halt_town_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_dindis_current_halt_town_id_halt_towns",
        "dindis",
        "halt_towns",
        ["current_halt_town_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_dindis_status", "dindis", ["status"])
    op.create_index("ix_dindis_last_ping_at", "dindis", ["last_ping_at"])
    op.create_check_constraint(
        "ck_dindis_route_fraction_range",
        "dindis",
        "route_fraction IS NULL OR (route_fraction >= 0 AND route_fraction <= 1)",
    )
    # A group of zero is a registration nobody finished; a group of fifty
    # thousand is a typo that would swamp a halt town's readiness maths.
    op.create_check_constraint(
        "ck_dindis_expected_count_range",
        "dindis",
        "expected_count > 0 AND expected_count <= 20000",
    )

    # ----------------------------------------------------------- dindi_pings
    op.add_column("dindi_pings", sa.Column("route_fraction", sa.Float(), nullable=True))
    op.add_column("dindi_pings", sa.Column("off_route_m", sa.Float(), nullable=True))
    # The pace estimate reads one Dindi's pings over a 90-minute window; without
    # this it is a scan of every Dindi's history inside each chunk.
    op.create_index("ix_dindi_pings_dindi_time_desc", "dindi_pings", ["dindi_id", sa.text("time DESC")])

    # ------------------------------------------------------------ halt_towns
    op.add_column("halt_towns", sa.Column("readiness_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("halt_towns", sa.Column("readiness_updated_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_halt_towns_readiness_updated_by_users",
        "halt_towns",
        "users",
        ["readiness_updated_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_halt_towns_route_sequence", "halt_towns", ["route_id", "sequence"])
    op.create_check_constraint(
        "ck_halt_towns_provisioning_non_negative",
        "halt_towns",
        "water_points >= 0 AND sanitation_units >= 0 AND medical_camps >= 0",
    )

    # ---------------------------------------------------------------- alerts
    # A Palkhi alert has no zone — the Wari is 250 km of road, and what an
    # operator clicks through to is a Dindi and the town it is about to reach.
    # Explicit foreign keys rather than a generic subject_type/subject_id pair,
    # for the same reason `zone_id` is one: "every alert about Saswad tonight"
    # should be an indexed query.
    op.add_column("alerts", sa.Column("dindi_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("alerts", sa.Column("halt_town_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_alerts_dindi_id_dindis", "alerts", "dindis", ["dindi_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_alerts_halt_town_id_halt_towns",
        "alerts",
        "halt_towns",
        ["halt_town_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_alerts_dindi_id", "alerts", ["dindi_id"])
    op.create_index("ix_alerts_halt_town_id", "alerts", ["halt_town_id"])

    # -------------------------------------------------------- dindi_schedule
    op.create_table(
        "dindi_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dindi_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("halt_town_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("planned_arrival", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_departure", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_arrival", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_departure", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["dindi_id"], ["dindis.id"], name=op.f("fk_dindi_schedule_dindi_id_dindis"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["halt_town_id"],
            ["halt_towns.id"],
            name=op.f("fk_dindi_schedule_halt_town_id_halt_towns"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dindi_schedule")),
        sa.UniqueConstraint("dindi_id", "sequence", name="uq_dindi_schedule_sequence"),
        sa.UniqueConstraint("dindi_id", "halt_town_id", name="uq_dindi_schedule_town"),
        sa.CheckConstraint(
            "planned_departure IS NULL OR planned_departure >= planned_arrival",
            name="ck_dindi_schedule_departure_after_arrival",
        ),
    )
    op.create_index(op.f("ix_dindi_schedule_dindi_id"), "dindi_schedule", ["dindi_id"])
    op.create_index(op.f("ix_dindi_schedule_halt_town_id"), "dindi_schedule", ["halt_town_id"])
    # "Who is arriving at this town tonight" — the readiness board's one query.
    op.create_index("ix_dindi_schedule_town_arrival", "dindi_schedule", ["halt_town_id", "planned_arrival"])

    # ------------------------------------------------------ assistant_turns
    op.create_table(
        "assistant_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=False, server_default="mr"),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(length=12), nullable=False),
        sa.Column("refusal_reason", sa.String(length=40), nullable=True),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_assistant_turns_actor_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistant_turns")),
    )
    op.create_index(op.f("ix_assistant_turns_session_id"), "assistant_turns", ["session_id"])
    op.create_index(op.f("ix_assistant_turns_actor_id"), "assistant_turns", ["actor_id"])
    op.create_index(op.f("ix_assistant_turns_created_at"), "assistant_turns", ["created_at"])
    op.create_index(op.f("ix_assistant_turns_outcome"), "assistant_turns", ["outcome"])
    op.create_index("ix_assistant_turns_outcome_created", "assistant_turns", ["outcome", "created_at"])

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

    op.drop_index("ix_assistant_turns_outcome_created", table_name="assistant_turns")
    op.drop_index(op.f("ix_assistant_turns_outcome"), table_name="assistant_turns")
    op.drop_index(op.f("ix_assistant_turns_created_at"), table_name="assistant_turns")
    op.drop_index(op.f("ix_assistant_turns_actor_id"), table_name="assistant_turns")
    op.drop_index(op.f("ix_assistant_turns_session_id"), table_name="assistant_turns")
    op.drop_table("assistant_turns")

    op.drop_index("ix_alerts_halt_town_id", table_name="alerts")
    op.drop_index("ix_alerts_dindi_id", table_name="alerts")
    op.drop_constraint("fk_alerts_halt_town_id_halt_towns", "alerts", type_="foreignkey")
    op.drop_constraint("fk_alerts_dindi_id_dindis", "alerts", type_="foreignkey")
    op.drop_column("alerts", "halt_town_id")
    op.drop_column("alerts", "dindi_id")

    op.drop_index("ix_dindi_schedule_town_arrival", table_name="dindi_schedule")
    op.drop_index(op.f("ix_dindi_schedule_halt_town_id"), table_name="dindi_schedule")
    op.drop_index(op.f("ix_dindi_schedule_dindi_id"), table_name="dindi_schedule")
    op.drop_table("dindi_schedule")

    op.drop_constraint("ck_halt_towns_provisioning_non_negative", "halt_towns", type_="check")
    op.drop_index("ix_halt_towns_route_sequence", table_name="halt_towns")
    op.drop_constraint("fk_halt_towns_readiness_updated_by_users", "halt_towns", type_="foreignkey")
    op.drop_column("halt_towns", "readiness_updated_by")
    op.drop_column("halt_towns", "readiness_updated_at")

    op.drop_index("ix_dindi_pings_dindi_time_desc", table_name="dindi_pings")
    op.drop_column("dindi_pings", "off_route_m")
    op.drop_column("dindi_pings", "route_fraction")

    op.drop_constraint("ck_dindis_expected_count_range", "dindis", type_="check")
    op.drop_constraint("ck_dindis_route_fraction_range", "dindis", type_="check")
    op.drop_index("ix_dindis_last_ping_at", table_name="dindis")
    op.drop_index("ix_dindis_status", table_name="dindis")
    op.drop_constraint("fk_dindis_current_halt_town_id_halt_towns", "dindis", type_="foreignkey")
    op.drop_column("dindis", "current_halt_town_id")
    op.drop_column("dindis", "route_fraction")
    op.drop_column("dindis", "last_battery")
    op.drop_column("dindis", "last_ping_at")
    op.drop_column("dindis", "started_at")
    op.drop_column("dindis", "status")
