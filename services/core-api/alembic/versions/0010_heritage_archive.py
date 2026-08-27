"""The Wari heritage archive.

Revision ID: 0010_heritage_archive
Revises: 0009_accessibility
Create Date: 2026-08-28

Track 1 item 5. One table, and the shape of it is an argument:

* `body_mr` is NOT NULL and `body_en` is nullable. The inverse of most schemas
  in most software, and correct here — an archive of the Wari whose canonical
  text is a translation has already lost the thing it set out to preserve.
* `status` defaults to `pending`, so a row that arrives without one is
  unpublished. The gate fails closed.
* `era` is a string. The archive does not have date precision and pretending
  otherwise with a DATE column would turn "18th century, approximately" into a
  fabricated January the 1st.
* `media_uri`, not bytes. This table is read on every archive page; a BYTEA
  column would make that read carry megabytes of audio it does not need.

The foreign keys to `halt_towns` and `dindis` are both `SET NULL`: a Dindi that
stops registering next year must not take its own history with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_heritage_archive"
down_revision: str | None = "0009_accessibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "heritage_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("title_mr", sa.String(length=200), nullable=False),
        sa.Column("title_en", sa.String(length=200), nullable=True),
        # Marathi required, English optional. See the module docstring.
        sa.Column("body_mr", sa.Text(), nullable=False),
        sa.Column("body_en", sa.Text(), nullable=True),
        sa.Column("attribution", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=300), nullable=True),
        sa.Column("era", sa.String(length=60), nullable=True),
        sa.Column("media_uri", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=True),
        sa.Column("halt_town_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dindi_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("contributed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contributed_by_name", sa.String(length=120), nullable=True),
        # Fails closed: a row with no status is unpublished.
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        # A Dindi that stops registering must not take its history with it.
        sa.ForeignKeyConstraint(["halt_town_id"], ["halt_towns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dindi_id"], ["dindis.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contributed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_heritage_items_kind", "heritage_items", ["kind"])
    op.create_index("ix_heritage_items_status", "heritage_items", ["status"])
    op.create_index("ix_heritage_items_halt_town_id", "heritage_items", ["halt_town_id"])
    op.create_index("ix_heritage_items_dindi_id", "heritage_items", ["dindi_id"])
    op.create_index("ix_heritage_items_contributed_by", "heritage_items", ["contributed_by"])
    op.create_index("ix_heritage_items_published_at", "heritage_items", ["published_at"])
    op.create_index("ix_heritage_items_created_at", "heritage_items", ["created_at"])
    # The archive page: published items of a kind, newest first.
    op.create_index("ix_heritage_status_kind", "heritage_items", ["status", "kind"])
    op.create_index("ix_heritage_published", "heritage_items", ["status", "published_at"])


def downgrade() -> None:
    op.drop_table("heritage_items")
