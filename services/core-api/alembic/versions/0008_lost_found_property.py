"""Lost and found property: the register, and the suggested pairings.

Revision ID: 0008_lost_found_property
Revises: 0007_palkhi_and_assistant
Create Date: 2026-08-27

Track 1 item 2 asks for a "Digital Lost-and-Found Management System". Half of it
already existed as `missing_persons` — the half where the lost thing is a person
and is also looking for you. This adds the other half.

Two tables rather than one, and the split is not bookkeeping:

* `lost_found_items` holds both sides of the desk in one table, discriminated by
  `kind`. One table because a lost report and a found item are the same shape
  and are constantly compared against each other; a self-join across a shared
  table is the query this module is made of, and two tables would make every
  one of those a union.
* `lost_found_matches` holds *suggestions* and what a human decided about them.
  Separate because a pairing has its own lifecycle — suggested, then accepted or
  rejected by a named person at a time — and because the rejected rows are the
  only evidence the scoring is wrong in a particular way. Storing the decision
  on the item would keep the last one and lose that.

The unique constraint on (lost_item_id, found_item_id) is what makes the
suggestion sweep idempotent: re-running it must not create a second row, and
must not silently reopen a pair a volunteer already rejected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_lost_found_property"
down_revision: str | None = "0007_palkhi_and_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lost_found_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("colour", sa.String(length=24), nullable=True),
        # Never returned by the public search. The fraud model in
        # `models/lostfound.py` turns on this column staying private.
        sa.Column("distinguishing_marks", sa.Text(), nullable=True),
        sa.Column("photo_uri", sa.Text(), nullable=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("custody_facility_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reported_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reporter_phone_hash", sa.String(length=64), nullable=True),
        sa.Column("language", sa.String(length=5), nullable=False, server_default="mr"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("matched_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by_name", sa.String(length=120), nullable=True),
        sa.Column("claimed_by_phone_hash", sa.String(length=64), nullable=True),
        sa.Column("handed_over_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handover_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["custody_facility_id"], ["facilities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reported_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["handed_over_by"], ["users.id"], ondelete="SET NULL"),
        # Self-reference: the counterpart record once a human confirms the pair.
        sa.ForeignKeyConstraint(["matched_item_id"], ["lost_found_items.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_lost_found_items_reference", "lost_found_items", ["reference"], unique=True)
    op.create_index("ix_lost_found_items_kind", "lost_found_items", ["kind"])
    op.create_index("ix_lost_found_items_category", "lost_found_items", ["category"])
    op.create_index("ix_lost_found_items_status", "lost_found_items", ["status"])
    op.create_index("ix_lost_found_items_zone_id", "lost_found_items", ["zone_id"])
    op.create_index("ix_lost_found_items_occurred_at", "lost_found_items", ["occurred_at"])
    op.create_index("ix_lost_found_items_reported_at", "lost_found_items", ["reported_at"])
    op.create_index("ix_lost_found_items_purge_after", "lost_found_items", ["purge_after"])
    op.create_index(
        "ix_lost_found_items_custody_facility_id", "lost_found_items", ["custody_facility_id"]
    )
    op.create_index("ix_lost_found_items_reported_by", "lost_found_items", ["reported_by"])
    op.create_index(
        "ix_lost_found_items_reporter_phone_hash", "lost_found_items", ["reporter_phone_hash"]
    )
    # The desk's working list: "open found items" and "open lost reports".
    op.create_index("ix_lostfound_kind_status", "lost_found_items", ["kind", "status"])
    # The matching sweep: same category, near in time.
    op.create_index(
        "ix_lostfound_category_occurred", "lost_found_items", ["category", "occurred_at"]
    )

    op.create_table(
        "lost_found_matches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lost_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("found_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("suggested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lost_item_id"], ["lost_found_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["found_item_id"], ["lost_found_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], ondelete="SET NULL"),
        # Makes the suggestion sweep idempotent, and stops a rejected pair being
        # silently re-suggested as pending on the next run.
        sa.UniqueConstraint("lost_item_id", "found_item_id", name="uq_lostfound_pair"),
    )
    op.create_index("ix_lost_found_matches_lost_item_id", "lost_found_matches", ["lost_item_id"])
    op.create_index("ix_lost_found_matches_found_item_id", "lost_found_matches", ["found_item_id"])
    op.create_index("ix_lost_found_matches_decision", "lost_found_matches", ["decision"])
    op.create_index("ix_lostfound_match_decision", "lost_found_matches", ["decision", "score"])


def downgrade() -> None:
    op.drop_table("lost_found_matches")
    op.drop_table("lost_found_items")
