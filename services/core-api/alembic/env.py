"""Alembic environment.

Runs synchronously (psycopg3 speaks both protocols), so no asyncio plumbing is
needed here.  GeoAlchemy2's spatial indexes are managed by the extension itself,
so autogenerate is told to ignore them rather than fight over them each run.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("DATABASE_URL", settings.database_url)
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

# TimescaleDB creates internal chunk tables and PostGIS creates helper views;
# neither belongs in a migration diff.
IGNORED_TABLES = {
    "spatial_ref_sys",
    "geography_columns",
    "geometry_columns",
    "raster_columns",
    "raster_overviews",
}
IGNORED_SCHEMAS = {
    "_timescaledb_internal",
    "_timescaledb_catalog",
    "_timescaledb_config",
    "timescaledb_information",
    "tiger",
    "topology",
}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        if name in IGNORED_TABLES:
            return False
        if getattr(obj, "schema", None) in IGNORED_SCHEMAS:
            return False
    # GeoAlchemy2's implicit idx_<table>_<column> indexes; the models declare
    # their own named GIST indexes instead.
    return not (type_ == "index" and name and name.startswith("idx_") and name.endswith("_geom"))


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
