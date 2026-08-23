"""Add the early-reslot opt-in flag to passes.

Revision ID: 0004_pass_early_reslot
Revises: 0003_hypertables_and_guards
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_pass_early_reslot"
down_revision: str | None = "0003_hypertables_and_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Default false: moving someone's darshan earlier is opt-in, never assumed.
    op.add_column(
        "passes",
        sa.Column("allow_early_reslot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Reslotting scans "unfulfilled passes in slots that have not started yet";
    # without this it is a sequential scan over the whole day's passes every
    # five minutes.
    op.create_index(
        "ix_passes_status_slot",
        "passes",
        ["status", "slot_id"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_passes_status_slot", table_name="passes")
    op.drop_column("passes", "allow_early_reslot")
