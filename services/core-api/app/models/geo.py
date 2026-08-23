"""Spatial entities: zones, cameras, tripwires, facilities, gates."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Zone(Base, TimestampMixin):
    __tablename__ = "zones"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(160), nullable=False)

    geom: Mapped[Any] = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False)
    # Ground-truth area.  Density is people / area_m2, so a wrong number here
    # makes every alert wrong — it is set during calibration, not guessed.
    area_m2: Mapped[float] = mapped_column(Float, nullable=False)
    capacity_persons: Mapped[int] = mapped_column(Integer, nullable=False)
    zone_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # temple_core | corridor | ghat | queue | halt_town | approach_road | facility
    parent_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_zones_geom", "geom", postgresql_using="gist"),
        Index("ix_zones_type_active", "zone_type", "is_active"),
    )


class Camera(Base, TimestampMixin):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    stream_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 3x3 homography mapping image pixels to ground-plane metres.  Without it
    # the density figure is fiction (Section 4/M2).
    homography_matrix: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    calibrated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="offline")
    # online | degraded | offline
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    is_tripwire_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def is_calibrated(self) -> bool:
        return self.homography_matrix is not None


class Tripwire(Base, TimestampMixin):
    """A directional line at a restricted gate (Section 4/M5)."""

    __tablename__ = "tripwires"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    camera_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("gates.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # {"points": [[x1,y1],[x2,y2]]} in normalised image coordinates
    geometry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    restricted_direction: Mapped[str] = mapped_column(String(8), nullable=False)  # in | out
    # {"windows": [{"days": [...], "from": "HH:MM", "to": "HH:MM"}]}
    active_schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Gate(Base, TimestampMixin):
    __tablename__ = "gates"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(160), nullable=False)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=True)
    throughput_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=1500)
    is_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_gates_location", "location", postgresql_using="gist"),)


class Facility(Base, TimestampMixin):
    __tablename__ = "facilities"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # toilet | water | medical | food | rest_zone | lost_and_found | help_desk | charging
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    name_mr: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[Any] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="operational")
    # operational | overloaded | out_of_service
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes_mr: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_facilities_location", "location", postgresql_using="gist"),)
