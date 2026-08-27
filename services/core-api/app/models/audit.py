"""Append-only audit log and runtime configuration.

The migration installs a trigger that raises on UPDATE and DELETE against
`audit_log`.  A grant can be changed by whoever holds the role; a trigger has to
be dropped, and dropping it is itself a schema change someone can see.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (Index("ix_audit_actor_created", "actor_id", "created_at"),)


class SystemConfig(Base, TimestampMixin):
    """Operator-tunable values that must not require a deploy to change."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


#: Seeded on first migration.  Keys are referenced by name from services.
DEFAULT_CONFIG: dict[str, tuple[Any, str]] = {
    "temple_throughput_per_hour": (6000, "Planned darshan throughput used to size slot capacity."),
    "walkin_reserve_pct": (0.25, "Share of every slot held back for pilgrims without smartphones."),
    "slot_minutes": (30, "Length of one darshan slot."),
    "day_start": ("04:00", "First slot start time."),
    "day_end": ("23:00", "Last slot end time."),
    "pass_expiry_grace_minutes": (45, "Minutes after slot end before an unscanned pass expires."),
    "reslot_deviation_pct": (0.20, "Throughput deviation that triggers dynamic reslotting."),
    "alert_escalate_seconds": (60, "Unacknowledged CRITICAL alert escalates visually after this."),
    "alert_page_seconds": (180, "Unacknowledged CRITICAL alert pages the next role after this."),
    "breach_retention_days": (90, "Retention for breach evidence clips."),
    "missing_person_photo_retention_days": (30, "Retention for missing-person photos after closure."),
    "stagnation_alert_threshold": (0.70, "Stagnation index that alerts when density is above 3.0."),
    "counterflow_alert_threshold": (0.35, "Counter-flow ratio that indicates turbulent opposing streams."),
    # Phase 3 — crowd engine.  Note what is *not* here: the density bands
    # themselves.  Those are published crowd-safety figures and live in code, so
    # nobody under pressure can make a critical zone look safe by editing a row.
    "alert_cooldown_seconds": (180, "How long an open alert absorbs repeat triggers before it re-notifies."),
    "camera_offline_seconds": (120, "Silence from a camera for this long marks it offline."),
    "crowd_window_seconds": (10, "Aggregation window the AI engine reports on."),
    "crowd_sample_fps": (2.0, "Frames per second sampled from each stream."),
    "sim_baseline_multiplier": (1.0, "Scales the simulation engine's diurnal baseline."),
    # Phase 8 — forecasting.  Note what is *not* here, again: the density bands
    # a forecast is classified against.  A predicted 4.2 p/m² is HIGH for the
    # same published reason a measured 4.2 is, and neither is editable.
    "forecast_interval_seconds": (300, "How often the engine publishes a new set of forecasts."),
    "forecast_stale_seconds": (900, "A forecast older than this is reported unavailable rather than shown."),
    "forecast_retention_days": (30, "How long forecasts are kept for scoring against what happened."),
    "forecast_alert_horizon_minutes": (60, "The only horizon allowed to raise a forecast_high alert."),
    # Phase 9 — Palkhi tracking.  The halt-town provisioning ratios *are* here,
    # unlike the density bands, and the difference is worth stating: a density
    # band is a published crowd-safety limit, while "one water point per 250
    # walkers" is a planning convention a district administration will adjust.
    # The readiness board prints the ratio it used, so changing one shows up in
    # the output rather than hiding behind it.
    "dindi_deviation_minutes": (45, "Schedule deviation that notifies the next halt town (Section 4/M8)."),
    "dindi_ping_interval_seconds": (60, "How often a Dindi's designated device should report."),
    "dindi_signal_lost_minutes": (20, "No ping for this long and the Dindi reads as signal_lost, not walking."),
    "dindi_pace_window_minutes": (90, "Window the walking pace is averaged over."),
    "dindi_off_route_alert_m": (500, "Distance from the route line that means the group has left the route."),
    "dindi_halt_arrival_radius_m": (800, "Distance from a halt town centre that counts as arrival."),
    "halt_water_points_per_1000": (4.0, "Water points per 1000 expected pilgrims (1 per 250)."),
    "halt_sanitation_units_per_1000": (10.0, "Sanitation units per 1000 expected pilgrims (1 per 100)."),
    "halt_medical_camps_per_10000": (1.0, "Medical camps per 10000 expected pilgrims at a halt town."),
    # Phase 9 — assistant (Section 13).
    "assistant_enabled": (True, "Master switch for the pilgrim assistant."),
    "assistant_max_turns_per_hour": (30, "Per-session turn ceiling."),
    "assistant_turn_retention_days": (90, "How long assistant transcripts are kept for review."),
}
