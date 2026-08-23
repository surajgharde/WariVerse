"""Palkhi and Dindi tracking (Section 4/M8).

The Wari is 250 km over 18 days; the temple is only the last day.  These tables
are what extend the product from a temple tool to a Wari tool.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Route(Base, TimestampMixin):
    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(160), nullable=False)
    origin: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. Alandi, Dehu
    path: Mapped[Any | None] = mapped_column(Geometry("LINESTRING", srid=4326, spatial_index=False), nullable=True)
    total_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_routes_path", "path", postgresql_using="gist"),)


class Dindi(Base, TimestampMixin):
    __tablename__ = "dindis"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(200), nullable=False)
    leader_name: Mapped[str] = mapped_column(String(120), nullable=False)
    leader_phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("routes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # One designated device per Dindi sends position (Section 4/M8).
    tracking_device_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DindiPing(Base):
    """Position pings.  A Dindi is a group, not a person — this is not
    individual tracking."""

    __tablename__ = "dindi_pings"

    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    dindi_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("dindis.id", ondelete="CASCADE"), primary_key=True
    )
    location: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    battery: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_kmph: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_dindi_pings_location", "location", postgresql_using="gist"),)


class HaltTown(Base, TimestampMixin):
    __tablename__ = "halt_towns"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(160), nullable=False)
    route_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("routes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    geom: Mapped[Any | None] = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=False), nullable=True)
    centroid: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=True)

    expected_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_headcount: Mapped[int | None] = mapped_column(Integer, nullable=True)

    water_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sanitation_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medical_camps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    readiness_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # ready | partial | not_ready | unknown
    readiness_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    contacts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_halt_towns_centroid", "centroid", postgresql_using="gist"),
        Index("ix_halt_towns_geom", "geom", postgresql_using="gist"),
    )
