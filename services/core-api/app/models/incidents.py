"""Incidents, responders and missing persons (Section 4/M4)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class IncidentType(StrEnum):
    MEDICAL = "medical"
    MISSING_PERSON = "missing_person"
    CROWD_CRUSH_RISK = "crowd_crush_risk"
    FIRE = "fire"
    STRUCTURAL = "structural"
    LOST_ITEM = "lost_item"
    FACILITY_FAILURE = "facility_failure"
    SECURITY = "security"
    OTHER = "other"


class IncidentSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class IncidentStatus(StrEnum):
    REPORTED = "reported"
    TRIAGED = "triaged"
    DISPATCHED = "dispatched"
    ON_SCENE = "on_scene"
    RESOLVED = "resolved"
    CLOSED = "closed"


#: Minutes from report to first responder contact, per severity (Section 4/M4).
SLA_MINUTES: dict[IncidentSeverity, int] = {
    IncidentSeverity.CRITICAL: 3,
    IncidentSeverity.HIGH: 10,
    IncidentSeverity.NORMAL: 30,
    IncidentSeverity.LOW: 120,
}


class Incident(Base, TimestampMixin):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(12), unique=True, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=IncidentStatus.REPORTED, index=True)

    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )
    location: Mapped[Any | None] = mapped_column(Geometry("POINT", srid=4326, spatial_index=False), nullable=True)

    reported_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reporter_phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # pilgrim_sos | volunteer_report | ai_alert | control_room | phone_call

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_note_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    sla_breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigned_responder_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("responders.id", ondelete="SET NULL"), nullable=True
    )
    # Set when the SOS arrived from an offline queue, so an operator can see the
    # report is older than its arrival time.
    client_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_incidents_status_severity_created", "status", "severity", "created_at"),
        Index("ix_incidents_location", "location", postgresql_using="gist"),
    )


class IncidentEvent(Base):
    """Append-only timeline of one incident."""

    __tablename__ = "incident_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Responder(Base, TimestampMixin):
    __tablename__ = "responders"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    call_sign: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    unit_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    # ambulance | medical_team | police | fire | volunteer_squad | help_desk
    current_location: Mapped[Any | None] = mapped_column(
        Geometry("POINT", srid=4326, spatial_index=False), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="available", index=True)
    # available | assigned | on_scene | off_duty
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_responders_location", "current_location", postgresql_using="gist"),)


class MissingPerson(Base, TimestampMixin):
    """The highest-frequency real incident at Wari (Section 5, E2)."""

    __tablename__ = "missing_persons"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    contact_phone_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False, default="mr")

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    # open | sighted | reunited | closed_unresolved
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Photos auto-purge 30 days after case closure (Section 12).
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
