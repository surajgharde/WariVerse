"""Enable PostGIS, TimescaleDB and pgcrypto.

Revision ID: 0001_extensions
Revises:
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostGIS: zone polygons, incident points, GIST indexes.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    # TimescaleDB: density_readings and dindi_pings become hypertables.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    # pgcrypto: gen_random_uuid() for server-side defaults.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    # Deliberately not dropping extensions — other schemas in the same database
    # may depend on them, and dropping PostGIS cascades into data loss.
    pass
