"""Palkhi and Dindi tracking (Section 4/M8).

The Wari is 250 km over 18 days; the temple is only the last day.  These tables
are what extend the product from a temple tool to a Wari tool.

One thing to hold on to while reading this file: **a Dindi is a group, not a
person.**  Every position here is the position of one designated volunteer
phone, standing in for a walking group of a few hundred.  Nothing in this
module tracks an individual, and nothing in it should ever be extended to.
That is not a privacy nicety bolted on afterwards — it is what makes an
18-day, 250 km tracking system something a temple trust can actually deploy
under the DPDP Act (Section 12, E9).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DindiStatus(StrEnum):
    """Where a Dindi is in its walk.

    `SIGNAL_LOST` is deliberately a status and not a flag.  A Dindi whose phone
    has gone quiet is not "walking" — the system does not know what it is doing,
    and a board that renders the last known position as a current one is the
    same lie as rendering a stale density reading as live (Section 4/M7).
    """

    REGISTERED = "registered"  # enrolled, has not started
    WALKING = "walking"
    HALTED = "halted"  # at a scheduled halt town
    SIGNAL_LOST = "signal_lost"  # the phone is silent; the group is not lost
    ARRIVED = "arrived"  # reached Pandharpur
    WITHDRAWN = "withdrawn"


class HaltReadiness(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


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
    # One designated device per Dindi sends position (Section 4/M8).  Enforced
    # rather than assumed: a ping whose device id does not match this one is
    # refused, so a second volunteer who installs the app cannot silently make
    # the Palkhi appear to be in two places.
    tracking_device_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DindiStatus.REGISTERED, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Denormalised from the newest ping.  The readiness board reads every active
    # Dindi on every poll; without this it would be one hypertable scan per
    # Dindi per refresh to answer "is this thing still reporting".
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_battery: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Fraction of the route walked, 0..1, from the newest ping.  Derived, and
    #: null when the route has no path to measure against.
    route_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_halt_town_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("halt_towns.id", ondelete="SET NULL"), nullable=True
    )


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

    #: Position along the route, 0..1, resolved at write time with
    #: `ST_LineLocatePoint`.  Stored rather than recomputed because the pace
    #: estimate is a difference between two of these, and recomputing both ends
    #: on every sweep would mean re-projecting the whole ping history hourly.
    #: Null when the Dindi's route has no path geometry.
    route_fraction: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: How far off the route line the phone was, in metres.  A volunteer who has
    #: stepped into a village for tea is not a Palkhi that has left the route,
    #: and the deviation rules need to be able to tell those apart.
    off_route_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (Index("ix_dindi_pings_location", "location", postgresql_using="gist"),)


class DindiScheduleStop(Base, TimestampMixin):
    """One Dindi's planned arrival at one halt town — the "planned halt
    schedule" of Section 4/M8.

    This is per Dindi rather than per route because that is how the Wari
    actually works: forty Dindis share the Alandi route and reach Saswad on
    different days, and a halt town's real question is not "when does the route
    arrive" but "who is arriving at me tonight, and how many of them".
    """

    __tablename__ = "dindi_schedule"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dindi_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("dindis.id", ondelete="CASCADE"), nullable=False, index=True
    )
    halt_town_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("halt_towns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    planned_arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_departure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when the Dindi's position first falls inside the town.  Kept even
    #: after departure: a review of next year's schedule is built out of the
    #: gap between these two columns, across eighteen days and forty Dindis.
    actual_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_departure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Overrides the Dindi's `expected_count` for this stop — groups shed and
    #: gather walkers along the way, and a town plans for the number that will
    #: actually sleep there.
    expected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("dindi_id", "sequence", name="uq_dindi_schedule_sequence"),
        UniqueConstraint("dindi_id", "halt_town_id", name="uq_dindi_schedule_town"),
        Index("ix_dindi_schedule_town_arrival", "halt_town_id", "planned_arrival"),
    )


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
    readiness_status: Mapped[str] = mapped_column(String(16), nullable=False, default=HaltReadiness.UNKNOWN)
    readiness_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    contacts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Who last confirmed the provisioning figures above, and when.  A board
    #: showing "8 water points" with no idea whether that was checked this
    #: morning or typed in last March is a board that reads as reassurance
    #: rather than as information.
    readiness_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    readiness_updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_halt_towns_centroid", "centroid", postgresql_using="gist"),
        Index("ix_halt_towns_geom", "geom", postgresql_using="gist"),
        Index("ix_halt_towns_route_sequence", "route_id", "sequence"),
    )
