"""Accessibility profiles, assistance requests, reserved seats, facility survey.

Revision ID: 0009_accessibility
Revises: 0008_lost_found_property
Create Date: 2026-08-28

Track 1 item 4. Four changes, and only one of them is a new table that matters
on its own:

* `accessibility_profiles` — what a pilgrim needs, declared once. One row per
  user, enforced by a unique constraint rather than by hope.
* `assistance_requests` — one ask for help, with a clock. Deliberately not a
  shape of `incidents`: see the note in `models/accessibility.py`.
* `slots.assisted_reserve` / `assisted_used` — the part that actually changes
  who gets darshan. Seats an ordinary booking cannot take.
* `facilities.accessibility` — a JSONB survey bag. `{}` means *unsurveyed*, and
  the pilgrim UI renders that as "not known" rather than as passable.

The reserve columns default to 0, so every slot that already exists keeps
exactly the availability arithmetic it had this morning. Turning the reserve on
is an administrator setting a number, not a migration silently re-planning a
day of darshan that pilgrims have already booked into.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_accessibility"
down_revision: str | None = "0008_lost_found_property"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accessibility_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "needs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("large_text", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("high_contrast", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("companion_phone_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # One profile per pilgrim. Two rows would mean two answers to "does this
        # person need a wheelchair", and the booking path would pick whichever
        # the query planner returned first.
        sa.UniqueConstraint("user_id", name="uq_accessibility_profile_user"),
    )

    op.create_table(
        "assistance_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference", sa.String(length=12), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("on_behalf_of", sa.String(length=120), nullable=True),
        sa.Column(
            "needs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("facility_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        # Stored rather than derived, so the board can sort on it in SQL — the
        # same choice `incidents.sla_due_at` makes.
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=5), nullable=False, server_default="mr"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["gate_id"], ["gates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["facility_id"], ["facilities.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_assistance_requests_reference", "assistance_requests", ["reference"], unique=True)
    op.create_index("ix_assistance_requests_requested_by", "assistance_requests", ["requested_by"])
    op.create_index("ix_assistance_requests_zone_id", "assistance_requests", ["zone_id"])
    op.create_index("ix_assistance_requests_status", "assistance_requests", ["status"])
    op.create_index("ix_assistance_requests_requested_at", "assistance_requests", ["requested_at"])
    op.create_index("ix_assistance_requests_sla_due_at", "assistance_requests", ["sla_due_at"])
    # The board's only query: open requests, most overdue first.
    op.create_index("ix_assistance_status_due", "assistance_requests", ["status", "sla_due_at"])
    op.create_index("ix_assistance_zone_status", "assistance_requests", ["zone_id", "status"])

    # Reserved darshan capacity. Zero everywhere until an administrator sets it,
    # so no slot anybody has already booked into changes its arithmetic.
    op.add_column(
        "slots",
        sa.Column("assisted_reserve", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "slots",
        sa.Column("assisted_used", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "assisted_within_reserve",
        "slots",
        "assisted_used <= assisted_reserve",
    )

    # `{}` means unsurveyed, and the pilgrim UI must say so rather than render
    # an unchecked step as a facility somebody in a wheelchair can reach.
    op.add_column(
        "facilities",
        sa.Column(
            "accessibility",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("facilities", "accessibility")
    op.drop_constraint("assisted_within_reserve", "slots", type_="check")
    op.drop_column("slots", "assisted_used")
    op.drop_column("slots", "assisted_reserve")
    op.drop_table("assistance_requests")
    op.drop_table("accessibility_profiles")
