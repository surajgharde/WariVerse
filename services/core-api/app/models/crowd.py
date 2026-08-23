"""Crowd telemetry and alerts.

`density_readings` becomes a TimescaleDB hypertable in the migration — it is
the highest-volume table in the system (40 cameras x 6 writes/minute) and needs
time partitioning from day one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DensityLevel(StrEnum):
    """Published crowd-safety bands (Section 4/M2)."""

    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


#: people per m² — the boundaries every alert in the system is derived from.
DENSITY_THRESHOLDS: dict[DensityLevel, float] = {
    DensityLevel.SAFE: 2.0,
    DensityLevel.MODERATE: 3.5,
    DensityLevel.HIGH: 5.0,
}


def classify_density(density: float) -> DensityLevel:
    if density < DENSITY_THRESHOLDS[DensityLevel.SAFE]:
        return DensityLevel.SAFE
    if density < DENSITY_THRESHOLDS[DensityLevel.MODERATE]:
        return DensityLevel.MODERATE
    if density < DENSITY_THRESHOLDS[DensityLevel.HIGH]:
        return DensityLevel.HIGH
    return DensityLevel.CRITICAL


class DensityReading(Base):
    """One 10-second aggregate for one zone.  No per-person data, ever."""

    __tablename__ = "density_readings"

    # Composite PK: a hypertable's partitioning column must be in the key.
    time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    zone_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True
    )

    person_count: Mapped[int] = mapped_column(Integer, nullable=False)
    density: Mapped[float] = mapped_column(Float, nullable=False)
    level: Mapped[str] = mapped_column(String(12), nullable=False)

    flow_dx: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # m/s east
    flow_dy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # m/s north
    # Fraction of tracks with near-zero velocity over 60s.  A stalled dense
    # crowd is the crush precursor — this fires before raw density does.
    stagnation_index: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    counterflow_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source: Mapped[str] = mapped_column(String(12), nullable=False)  # live | video | sim | manual
    camera_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_density_readings_zone_time", "zone_id", "time", postgresql_using="btree"),)


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    # density_critical | density_high | stagnation | counterflow | forecast_high
    # | camera_offline | sla_breach | breach_pending_review | throughput_drop
    severity: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("zones.id", ondelete="SET NULL"), nullable=True, index=True
    )

    trigger_metric: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_value: Mapped[float] = mapped_column(Float, nullable=False)
    threshold_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Section 0 rule 3: no AI output is presented as certainty.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_action_mr: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which rule in the recommendation table produced the action, so an
    # operator can see it came from a rule and not from a language model.
    rule_id: Mapped[str | None] = mapped_column(String(60), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertStatus.OPEN, index=True)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_alerts_status_severity_created", "status", "severity", "created_at"),)
